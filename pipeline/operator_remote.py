"""Small JSON-emitting helpers executed by the operator on the remote host."""

from __future__ import annotations

import argparse
import csv
from dataclasses import asdict
import hashlib
import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Optional

import requests
from dotenv import load_dotenv

from pipeline.run_approval import evaluate_run_approval
from pipeline.canonical_rebuild import rebuild_canonical
from pipeline.operator_summary import derive_current_metrics
from pipeline.review_workflow import (
    apply_review_decisions,
    review_status,
    run_automated_review,
    staging_commands,
)
from pipeline.review_ui import merge_human_decision_file
from pipeline.run_context import RunContext, finalize_run, validate_run_id


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = (
    "manifest.json",
    "review-index.json",
    "operator-summary.json",
    "stage0/listing_links.csv",
    "stage2/review_queue.csv",
    "stage2/reviewed.csv",
    "stage3/geocoded.csv",
    "stage3/geocode_review.csv",
    "stage3/canonical.csv",
    "review/review-decisions.jsonl",
    "review/remaining-human-review.csv",
    "review/auto-resolved.csv",
    "review/accepted-unknown.csv",
    "review/review-auto-summary.json",
)
STAGE_OUTPUTS = {
    "stage0": ("stage0/listing_links.csv",),
    "stage1": ("stage1/website_ready.csv",),
    "stage2": ("stage2/enriched.csv",),
    "manual_fixes": ("stage2/reviewed.csv",),
    "stage3": ("stage3/geocoded.csv",),
    "stage3_qc": ("stage3/canonical.csv", "stage3/geocode_review.csv"),
}


def _git(arguments: list[str]) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip()


def _fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _csv_row_count(path: Path) -> Optional[int]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as source:
            reader = csv.reader(source)
            if next(reader, None) is None:
                return None
            return sum(1 for _ in reader)
    except (OSError, UnicodeError, csv.Error):
        return None


def remote_preflight(
    *,
    ollama_endpoint: str,
    ollama_model: str,
    minimum_free_gb: float,
    require_services: bool = True,
) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    required_files = (
        "pipeline/run_context.py",
        "pipeline/ai_enricher.py",
        "pipeline/geocoder.py",
        "scripts/run_full_pipeline.ps1",
    )
    checks["repository"] = all((PROJECT_ROOT / item).is_file() for item in required_files)
    checks["python"] = Path(sys.executable).is_file()
    checks["python_executable"] = str(Path(sys.executable))
    missing_imports: list[str] = []
    for name in ("pandas", "requests", "dotenv", "bs4", "tqdm"):
        try:
            importlib.import_module(name)
        except ImportError:
            missing_imports.append(name)
    checks["python_imports"] = not missing_imports
    checks["missing_imports"] = missing_imports

    runs_root = PROJECT_ROOT / "data" / "runs"
    try:
        runs_root.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=runs_root, delete=True):
            pass
        writable = True
    except OSError:
        writable = False
    checks["runs_root_writable"] = writable
    free_gb = round(shutil.disk_usage(PROJECT_ROOT).free / (1024**3), 2)
    checks["free_disk_gb"] = free_gb
    checks["free_disk"] = free_gb >= minimum_free_gb

    if require_services:
        env_path = PROJECT_ROOT / ".env"
        try:
            loaded = load_dotenv(env_path) if env_path.is_file() else False
            del loaded
            secret_status = "configured" if os.getenv("GEOAPIFY_API_KEY") else "missing"
        except (OSError, UnicodeError):
            secret_status = "unreadable"
        ollama_error: Optional[str] = None
        installed_models: list[str] = []
        try:
            response = requests.get(ollama_endpoint.rstrip("/") + "/api/tags", timeout=5)
            response.raise_for_status()
            payload = response.json()
            installed_models = [
                str(item.get("name"))
                for item in payload.get("models", [])
                if isinstance(item, dict) and item.get("name")
            ]
            ollama_reachable = True
        except (requests.RequestException, ValueError, TypeError) as exc:
            ollama_reachable = False
            ollama_error = type(exc).__name__
    else:
        secret_status = "not_checked"
        ollama_error = None
        installed_models = []
        ollama_reachable = None
    checks["ollama_reachable"] = ollama_reachable
    checks["ollama_model"] = (
        ollama_model in installed_models if require_services else None
    )
    checks["ollama_error"] = ollama_error
    checks["geoapify"] = secret_status
    checks["service_checks_required"] = require_services
    checks["git_branch"] = _git(["branch", "--show-current"])
    checks["git_commit"] = _git(["rev-parse", "HEAD"])
    checks["git_dirty"] = bool(_git(["status", "--porcelain", "--untracked-files=no"]))
    checks["ok"] = all(
        (
            checks["repository"],
            checks["python"],
            checks["python_imports"],
            checks["runs_root_writable"],
            checks["free_disk"],
            not require_services or checks["geoapify"] == "configured",
            not require_services or checks["ollama_reachable"],
            not require_services or checks["ollama_model"],
            bool(checks["git_commit"]),
            not checks["git_dirty"],
        )
    )
    return checks


def inspect_run(run_id: str) -> dict[str, Any]:
    validate_run_id(run_id)
    root = PROJECT_ROOT / "data" / "runs" / run_id
    manifest_path = root / "manifest.json"
    if not root.is_dir() or not manifest_path.is_file():
        raise ValueError("Requested run does not exist or lacks manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("run_id") != run_id:
        raise ValueError("Requested run has an invalid manifest")
    artifacts: dict[str, Any] = {}
    paths_to_check = set(ARTIFACTS)
    for values in STAGE_OUTPUTS.values():
        paths_to_check.update(values)
    for relative in sorted(paths_to_check):
        path = root / Path(relative)
        artifacts[relative] = {
            "exists": path.is_file(),
            "size": path.stat().st_size if path.is_file() else None,
            "sha256": _fingerprint(path) if path.is_file() else None,
            "row_count": _csv_row_count(path) if path.is_file() and path.suffix == ".csv" else None,
        }
    evaluation = evaluate_run_approval(root)
    history = manifest.get("error_history", [])
    if not isinstance(history, list):
        history = []
    active_errors = manifest.get("errors", [])
    if not isinstance(active_errors, list):
        active_errors = []
    resolved = sum(
        isinstance(item, dict) and bool(item.get("resolved_at_utc"))
        for item in history
    )
    return {
        "run_id": run_id,
        "run_dir": str(root),
        "manifest": manifest,
        "artifacts": artifacts,
        "approval_summary": asdict(evaluation.summary),
        "approval_blocking_conditions": list(evaluation.blocking_conditions),
        "active_error_count": len(active_errors),
        "historical_resolved_errors": resolved,
        "current_metrics": derive_current_metrics(root, manifest),
    }


def reconcile_run(run_id: str) -> dict[str, Any]:
    """Repair manifest bookkeeping without executing a processing stage."""

    validate_run_id(run_id)
    root = PROJECT_ROOT / "data" / "runs" / run_id
    context = RunContext.resume(root)
    active_before = context.manifest.get("errors", [])
    before = len(active_before) if isinstance(active_before, list) else 0
    status, warnings = finalize_run(context)
    active_after = context.manifest.get("errors", [])
    after = len(active_after) if isinstance(active_after, list) else 0
    return {
        "ok": True,
        "run_id": run_id,
        "run_status": status,
        "resolved_error_count": before - after,
        "current_active_errors": after,
        "completion_warning_count": len(warnings),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    preflight = commands.add_parser("preflight")
    preflight.add_argument("--ollama-endpoint", required=True)
    preflight.add_argument("--ollama-model", required=True)
    preflight.add_argument("--minimum-free-gb", type=float, required=True)
    preflight.add_argument("--skip-service-checks", action="store_true")
    inspect = commands.add_parser("inspect")
    inspect.add_argument("--run-id", required=True)
    reconcile = commands.add_parser("reconcile")
    reconcile.add_argument("--run-id", required=True)
    rebuild = commands.add_parser("rebuild-canonical")
    rebuild.add_argument("--run-id", required=True)
    for name in (
        "review-auto",
        "review-status",
        "apply-review-decisions",
        "review-next-steps",
    ):
        review = commands.add_parser(name)
        review.add_argument("--run-id", required=True)
    merge = commands.add_parser("review-merge-decisions")
    merge.add_argument("--run-id", required=True)
    merge.add_argument("--incoming", required=True)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        if args.command == "preflight":
            result = remote_preflight(
                ollama_endpoint=args.ollama_endpoint,
                ollama_model=args.ollama_model,
                minimum_free_gb=args.minimum_free_gb,
                require_services=not args.skip_service_checks,
            )
        elif args.command == "inspect":
            result = inspect_run(args.run_id)
        elif args.command == "reconcile":
            result = reconcile_run(args.run_id)
        elif args.command == "rebuild-canonical":
            result = rebuild_canonical(
                PROJECT_ROOT / "data" / "runs" / validate_run_id(args.run_id)
            )
        elif args.command == "review-merge-decisions":
            run_dir = PROJECT_ROOT / "data" / "runs" / validate_run_id(args.run_id)
            review_dir = (run_dir / "review").resolve()
            incoming = (review_dir / Path(args.incoming).name).resolve()
            if (
                incoming.parent != review_dir
                or not incoming.name.startswith(".incoming-review-decisions-")
                or incoming.suffix != ".jsonl"
            ):
                raise ValueError("Incoming review decision path is unsafe")
            try:
                result = merge_human_decision_file(run_dir, incoming)
            finally:
                if incoming.is_file():
                    incoming.unlink()
        else:
            run_dir = PROJECT_ROOT / "data" / "runs" / validate_run_id(args.run_id)
            if args.command == "review-auto":
                result = run_automated_review(run_dir)
            elif args.command == "review-status":
                result = review_status(run_dir)
            elif args.command == "apply-review-decisions":
                result = apply_review_decisions(run_dir)
            else:
                status = review_status(run_dir)
                result = {
                    **status,
                    "commands": staging_commands(args.run_id)
                    if status.get("ready_for_approval")
                    else [],
                }
    except Exception as exc:
        result = {"ok": False, "error": type(exc).__name__, "message": str(exc)}
        print(json.dumps(result, sort_keys=True))
        raise SystemExit(1) from exc
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
