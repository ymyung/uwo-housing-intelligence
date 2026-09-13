"""Versioned pipeline-run directories and atomic manifest management."""

from __future__ import annotations

import argparse
import copy
from contextlib import contextmanager
import hashlib
import json
import os
import platform as platform_module
import re
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional
from uuid import uuid4


PENDING = "pending"
RUNNING = "running"
COMPLETED = "completed"
COMPLETED_WITH_WARNINGS = "completed_with_warnings"
FAILED = "failed"
SKIPPED = "skipped"

VALID_STATUSES = {
    PENDING,
    RUNNING,
    COMPLETED,
    COMPLETED_WITH_WARNINGS,
    FAILED,
    SKIPPED,
}
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class ManifestLockError(RuntimeError):
    """A manifest cannot be updated while another local writer holds its lock."""


@contextmanager
def manifest_lock(
    manifest_path: Path, *, timeout_seconds: float = 10.0
) -> Iterator[None]:
    """Serialize cooperating manifest writers with a cross-platform lock file."""

    if timeout_seconds < 0:
        raise ValueError("timeout_seconds must be non-negative")
    resolved = manifest_path.resolve()
    lock_path = resolved.with_name(f".{resolved.name}.lock")
    deadline = time.monotonic() + timeout_seconds
    descriptor: Optional[int] = None
    while descriptor is None:
        try:
            descriptor = os.open(
                lock_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError as error:
            if time.monotonic() >= deadline:
                raise ManifestLockError(
                    f"Manifest is locked by another writer: {resolved}"
                ) from error
            time.sleep(0.05)
    try:
        os.write(descriptor, f"pid={os.getpid()}\n".encode("ascii"))
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        yield
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_text(value: Optional[datetime] = None) -> str:
    return (value or utc_now()).isoformat().replace("+00:00", "Z")


def _git_value(arguments: list[str], cwd: Path) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=cwd,
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    return result.stdout.strip()


def get_git_metadata(cwd: Optional[Path] = None) -> tuple[Optional[str], Optional[bool]]:
    repository = (cwd or Path.cwd()).resolve()
    commit = _git_value(["rev-parse", "HEAD"], repository)
    if not commit:
        return None, None
    dirty_output = _git_value(["status", "--porcelain"], repository)
    return commit, bool(dirty_output) if dirty_output is not None else None


def generate_run_id(
    *, now: Optional[datetime] = None, git_commit: Optional[str] = None
) -> str:
    timestamp = (now or utc_now()).strftime("%Y%m%dT%H%M%S%fZ")
    suffix = git_commit[:7] if git_commit else uuid4().hex[:7]
    return f"{timestamp}_{suffix}"


def validate_run_id(run_id: str) -> str:
    if not RUN_ID_RE.fullmatch(run_id) or run_id in {".", ".."}:
        raise ValueError(f"Run ID is not filesystem-safe: {run_id!r}")
    return run_id


@dataclass(frozen=True)
class RunPaths:
    root: Path

    @property
    def manifest(self) -> Path:
        return self.root / "manifest.json"

    @property
    def stage0_dir(self) -> Path:
        return self.root / "stage0"

    @property
    def stage0_listing_links(self) -> Path:
        return self.stage0_dir / "listing_links.csv"

    @property
    def stage1_dir(self) -> Path:
        return self.root / "stage1"

    @property
    def stage1_details(self) -> Path:
        return self.stage1_dir / "details.csv"

    @property
    def stage1_merged(self) -> Path:
        return self.stage1_dir / "details_merged.csv"

    @property
    def stage1_website_ready(self) -> Path:
        return self.stage1_dir / "website_ready.csv"

    @property
    def stage1_checkpoint(self) -> Path:
        return self.stage1_dir / "checkpoint.csv"

    @property
    def stage2_dir(self) -> Path:
        return self.root / "stage2"

    @property
    def stage2_enriched(self) -> Path:
        return self.stage2_dir / "enriched.csv"

    @property
    def stage2_review_queue(self) -> Path:
        return self.stage2_dir / "review_queue.csv"

    @property
    def stage2_reviewed(self) -> Path:
        return self.stage2_dir / "reviewed.csv"

    @property
    def stage3_dir(self) -> Path:
        return self.root / "stage3"

    @property
    def stage3_geocoded(self) -> Path:
        return self.stage3_dir / "geocoded.csv"

    @property
    def stage3_geocode_review(self) -> Path:
        return self.stage3_dir / "geocode_review.csv"

    @property
    def stage3_canonical(self) -> Path:
        return self.stage3_dir / "canonical.csv"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    def create_directories(self) -> None:
        for directory in (
            self.root,
            self.stage0_dir,
            self.stage1_dir,
            self.stage2_dir,
            self.stage3_dir,
            self.logs_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as temporary:
            json.dump(payload, temporary, indent=2, ensure_ascii=False)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        for attempt in range(6):
            try:
                os.replace(temporary_path, path)
                break
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.05 * (2**attempt))
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


class RunContext:
    def __init__(self, paths: RunPaths, manifest: dict[str, Any]):
        self.paths = paths
        self.manifest = manifest

    @classmethod
    def create(
        cls,
        runs_root: Path = Path("data/runs"),
        *,
        run_id: Optional[str] = None,
        command: Optional[list[str]] = None,
        configuration: Optional[dict[str, Any]] = None,
        git_cwd: Optional[Path] = None,
    ) -> "RunContext":
        git_commit, git_dirty = get_git_metadata(git_cwd)
        selected_id = validate_run_id(run_id or generate_run_id(git_commit=git_commit))
        return cls.create_at(
            runs_root / selected_id,
            run_id=selected_id,
            command=command,
            configuration=configuration,
            git_commit=git_commit,
            git_dirty=git_dirty,
        )

    @classmethod
    def create_at(
        cls,
        run_dir: Path,
        *,
        run_id: Optional[str] = None,
        command: Optional[list[str]] = None,
        configuration: Optional[dict[str, Any]] = None,
        git_commit: Optional[str] = None,
        git_dirty: Optional[bool] = None,
        allow_existing_directory: bool = False,
    ) -> "RunContext":
        root = run_dir.resolve()
        paths = RunPaths(root)
        if paths.manifest.exists() or (
            root.exists() and any(root.iterdir()) and not allow_existing_directory
        ):
            raise FileExistsError(f"Run directory already contains data: {root}")

        paths.create_directories()
        now = utc_text()
        manifest: dict[str, Any] = {
            "run_id": validate_run_id(run_id or root.name),
            "created_at_utc": now,
            "updated_at_utc": now,
            "status": RUNNING,
            "git_commit": git_commit,
            "git_dirty": git_dirty,
            "hostname": socket.gethostname(),
            "platform": platform_module.platform(),
            "python_version": platform_module.python_version(),
            "command": command if command is not None else sys.argv,
            "configuration": configuration or {},
            "stages": {},
            "warnings": [],
            "errors": [],
            "error_history": [],
            "canonical_for_import": False,
        }
        context = cls(paths, manifest)
        context.save()
        return context

    @classmethod
    def resume(cls, run_dir: Path) -> "RunContext":
        paths = RunPaths(run_dir.resolve())
        if not paths.manifest.exists():
            raise FileNotFoundError(f"Run manifest not found: {paths.manifest}")
        with paths.manifest.open("r", encoding="utf-8") as manifest_file:
            manifest = json.load(manifest_file)
        return cls(paths, manifest)

    @classmethod
    def open_for_cli(
        cls,
        run_dir: Path,
        *,
        resume: bool,
        overwrite: bool,
        command: list[str],
        configuration: dict[str, Any],
    ) -> "RunContext":
        resolved = run_dir.resolve()
        manifest_path = resolved / "manifest.json"
        if resume:
            return cls.resume(resolved)
        if manifest_path.exists():
            if not overwrite:
                raise FileExistsError(
                    f"Run already exists; pass --resume or --overwrite: {resolved}"
                )
            return cls.resume(resolved)
        if resolved.exists() and any(resolved.iterdir()) and not overwrite:
            raise FileExistsError(
                f"Run directory is not empty; pass --overwrite: {resolved}"
            )
        git_commit, git_dirty = get_git_metadata()
        return cls.create_at(
            resolved,
            command=command,
            configuration=configuration,
            git_commit=git_commit,
            git_dirty=git_dirty,
            allow_existing_directory=overwrite,
        )

    def save(self) -> None:
        self.manifest["updated_at_utc"] = utc_text()
        with manifest_lock(self.paths.manifest):
            atomic_write_json(self.paths.manifest, self.manifest)

    def ensure_outputs_available(
        self, output_paths: Iterable[Path], *, allow_existing: bool = False
    ) -> None:
        existing = [path for path in output_paths if path.exists()]
        if existing and not allow_existing:
            joined = ", ".join(str(path) for path in existing)
            raise FileExistsError(f"Run outputs already exist: {joined}")

    def start_stage(
        self,
        stage_name: str,
        *,
        input_paths: Iterable[Path] = (),
        output_paths: Iterable[Path] = (),
    ) -> None:
        started = utc_now()
        self.manifest["status"] = RUNNING
        previous_stage = self.manifest["stages"].get(stage_name, {})
        previous_attempt = (
            previous_stage.get("attempt_number", 0)
            if isinstance(previous_stage, dict)
            else 0
        )
        self.manifest["stages"][stage_name] = {
            "status": RUNNING,
            "attempt_id": uuid4().hex,
            "attempt_number": previous_attempt + 1,
            "started_at_utc": utc_text(started),
            "completed_at_utc": None,
            "input_paths": [str(path) for path in input_paths],
            "output_paths": [str(path) for path in output_paths],
            "input_rows": None,
            "output_rows": None,
            "error_count": 0,
            "warning_count": 0,
            "duration_seconds": None,
        }
        self.save()

    def finish_stage(
        self,
        stage_name: str,
        *,
        input_rows: Optional[int] = None,
        output_rows: Optional[int] = None,
        warnings: Iterable[str] = (),
        error_count: int = 0,
        metrics: Optional[dict[str, Any]] = None,
    ) -> None:
        stage = self.manifest["stages"][stage_name]
        warning_list = list(warnings)
        completed = utc_now()
        started = datetime.fromisoformat(stage["started_at_utc"].replace("Z", "+00:00"))
        stage.update(
            {
                "status": COMPLETED_WITH_WARNINGS if warning_list else COMPLETED,
                "completed_at_utc": utc_text(completed),
                "input_rows": input_rows,
                "output_rows": output_rows,
                "error_count": error_count,
                "warning_count": len(warning_list),
                "duration_seconds": round((completed - started).total_seconds(), 3),
            }
        )
        if metrics:
            stage["metrics"] = metrics
        self._resolve_stage_errors(stage_name, completed)
        self.manifest["warnings"].extend(
            {"stage": stage_name, "message": warning} for warning in warning_list
        )
        self.save()

    def skip_stage(
        self,
        stage_name: str,
        *,
        reason: str,
        input_paths: Iterable[Path] = (),
        output_paths: Iterable[Path] = (),
        input_rows: Optional[int] = None,
        output_rows: Optional[int] = None,
    ) -> None:
        now = utc_text()
        self.manifest["stages"][stage_name] = {
            "status": SKIPPED,
            "started_at_utc": now,
            "completed_at_utc": now,
            "input_paths": [str(path) for path in input_paths],
            "output_paths": [str(path) for path in output_paths],
            "input_rows": input_rows,
            "output_rows": output_rows,
            "error_count": 0,
            "warning_count": 1,
            "duration_seconds": 0.0,
            "metrics": {"skip_reason": reason},
        }
        self.manifest["warnings"].append(
            {"stage": stage_name, "message": f"Stage skipped: {reason}"}
        )
        self.save()

    def fail_stage(
        self,
        stage_name: str,
        error: BaseException,
        *,
        metrics: Optional[dict[str, Any]] = None,
    ) -> None:
        stage = self.manifest["stages"][stage_name]
        completed = utc_now()
        started = datetime.fromisoformat(stage["started_at_utc"].replace("Z", "+00:00"))
        stage.update(
            {
                "status": FAILED,
                "completed_at_utc": utc_text(completed),
                "error_count": stage.get("error_count", 0) + 1,
                "duration_seconds": round((completed - started).total_seconds(), 3),
            }
        )
        if metrics:
            stage["metrics"] = metrics
        self.manifest["status"] = FAILED
        error_record = {
            "error_id": uuid4().hex,
            "attempt_id": stage.get("attempt_id"),
            "stage": stage_name,
            "type": type(error).__name__,
            "message": str(error),
            "at_utc": utc_text(completed),
            "active": True,
            "resolved_at_utc": None,
            "resolution_reason": None,
            "resolving_attempt_id": None,
            "resolving_completed_at_utc": None,
            "legacy_source": False,
        }
        self.manifest.setdefault("errors", []).append(copy.deepcopy(error_record))
        self.manifest.setdefault("error_history", []).append(error_record)
        self.save()

    def _resolve_stage_errors(self, stage_name: str, resolved_at: datetime) -> None:
        """Resolve active errors for one successfully completed stage only."""

        resolved_text = utc_text(resolved_at)
        active_errors = self.manifest.get("errors", [])
        if not isinstance(active_errors, list):
            return
        remaining: list[Any] = []
        resolved_records: list[dict[str, Any]] = []
        stage = self.manifest.get("stages", {}).get(stage_name, {})
        resolving_attempt_id = (
            stage.get("attempt_id") if isinstance(stage, dict) else None
        )
        duplicate_indexes: dict[tuple[Any, ...], int] = {}
        for error in active_errors:
            if isinstance(error, dict) and error.get("stage") == stage_name:
                resolved = copy.deepcopy(error)
                signature = (
                    resolved.get("stage"),
                    resolved.get("type"),
                    resolved.get("message"),
                    resolved.get("at_utc"),
                )
                occurrence = duplicate_indexes.get(signature, 0)
                duplicate_indexes[signature] = occurrence + 1
                if not resolved.get("error_id"):
                    resolved["error_id"] = canonical_error_identity(
                        resolved, occurrence
                    )
                    resolved["legacy_source"] = True
                else:
                    resolved.setdefault("legacy_source", False)
                resolved.setdefault("attempt_id", None)
                resolved["active"] = False
                resolved["resolved_at_utc"] = resolved_text
                resolved["resolution_reason"] = "stage_completed_successfully"
                resolved["resolving_attempt_id"] = resolving_attempt_id
                resolved["resolving_completed_at_utc"] = resolved_text
                resolved_records.append(resolved)
            else:
                remaining.append(error)
        if not resolved_records:
            return
        self.manifest["errors"] = remaining
        history = self.manifest.setdefault("error_history", [])
        if not isinstance(history, list):
            history = []
            self.manifest["error_history"] = history
        for resolved in resolved_records:
            error_id = resolved.get("error_id")
            matching = next(
                (
                    item
                    for item in history
                    if isinstance(item, dict)
                    and error_id
                    and item.get("error_id") == error_id
                ),
                None,
            )
            if matching is not None:
                matching.update(resolved)
            else:
                history.append(resolved)

    def reconcile_successful_stage_errors(self) -> int:
        """Resolve current errors made obsolete by successful current stages."""

        active_errors = self.manifest.get("errors", [])
        before = len(active_errors) if isinstance(active_errors, list) else 0
        stages = self.manifest.get("stages", {})
        if not isinstance(stages, dict):
            return 0
        for stage_name, stage in stages.items():
            if not isinstance(stage, dict) or stage.get("status") not in {
                COMPLETED,
                COMPLETED_WITH_WARNINGS,
            }:
                continue
            completed_text = stage.get("completed_at_utc")
            try:
                completed = datetime.fromisoformat(
                    str(completed_text).replace("Z", "+00:00")
                )
            except (TypeError, ValueError):
                completed = utc_now()
            self._resolve_stage_errors(stage_name, completed)
        remaining = self.manifest.get("errors", [])
        after = len(remaining) if isinstance(remaining, list) else 0
        return before - after


def canonical_error_identity(error: dict[str, Any], occurrence: int = 0) -> str:
    """Create a stable ID for legacy error evidence, including duplicates."""

    payload = json.dumps(
        {
            "stage": error.get("stage"),
            "type": error.get("type"),
            "message": error.get("message"),
            "at_utc": error.get("at_utc"),
            "occurrence": occurrence,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return "legacy-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def validate_discovery(
    discovered_count: int,
    *,
    minimum_count: int = 1,
    previous_successful_count: Optional[int] = None,
    substantial_drop_ratio: float = 0.5,
    repeated_page_detected: bool = False,
    maximum_page_limit_reached: bool = False,
) -> list[str]:
    if minimum_count < 0:
        raise ValueError("minimum_count must be non-negative")
    if not 0 < substantial_drop_ratio <= 1:
        raise ValueError("substantial_drop_ratio must be in (0, 1]")

    warnings: list[str] = []
    if discovered_count == 0:
        warnings.append("Discovery returned zero listings.")
    if discovered_count < minimum_count:
        warnings.append(
            f"Discovery count {discovered_count} is below minimum {minimum_count}."
        )
    if previous_successful_count is not None and previous_successful_count > 0:
        threshold = previous_successful_count * substantial_drop_ratio
        if discovered_count < threshold:
            warnings.append(
                "Discovery count fell substantially compared with the supplied "
                f"previous successful count ({discovered_count} vs {previous_successful_count})."
            )
    if repeated_page_detected:
        warnings.append("Discovery stopped after detecting a repeated result page.")
    if maximum_page_limit_reached:
        warnings.append("Discovery reached the configured maximum page limit.")
    return warnings


REQUIRED_STAGES = ("stage0", "stage1", "stage2", "manual_fixes", "stage3", "stage3_qc")
TERMINAL_STATUSES = {COMPLETED, COMPLETED_WITH_WARNINGS, FAILED, SKIPPED}


def determine_run_completion(
    context: RunContext,
    *,
    allowed_skipped_stages: Iterable[str] = ("stage2", "manual_fixes"),
) -> tuple[str, list[str]]:
    stages = context.manifest.get("stages", {})
    allowed_skips = set(allowed_skipped_stages)
    warnings: list[str] = []

    for stage_name in REQUIRED_STAGES:
        stage = stages.get(stage_name)
        if stage is None or stage.get("status") not in TERMINAL_STATUSES:
            return RUNNING, [f"Required stage is not terminal: {stage_name}"]
        if stage.get("status") == FAILED:
            return FAILED, [f"Required stage failed: {stage_name}"]
        if stage.get("status") == SKIPPED and stage_name not in allowed_skips:
            return FAILED, [f"Required stage cannot be skipped: {stage_name}"]
        if stage.get("status") == COMPLETED_WITH_WARNINGS:
            warnings.append(f"Stage completed with warnings: {stage_name}")

    if not context.paths.stage3_canonical.exists():
        return FAILED, ["Stage 3 canonical output is missing."]

    comparisons = (
        ("stage0", "stage1"),
        ("stage1", "stage2"),
        ("stage2", "manual_fixes"),
        ("manual_fixes", "stage3"),
        ("stage3", "stage3_qc"),
    )
    for previous_name, next_name in comparisons:
        previous = stages[previous_name]
        following = stages[next_name]
        previous_rows = previous.get("output_rows")
        next_input_rows = following.get("input_rows")
        if (
            previous_rows is not None
            and next_input_rows is not None
            and previous_rows != next_input_rows
        ):
            return FAILED, [
                f"Row-count mismatch: {previous_name} output={previous_rows}, "
                f"{next_name} input={next_input_rows}."
            ]

    qc = stages["stage3_qc"]
    if qc.get("input_rows") != qc.get("output_rows"):
        return FAILED, ["Stage 3 QC input/output row counts are inconsistent."]

    for stage_name in REQUIRED_STAGES:
        metrics = stages[stage_name].get("metrics", {})
        for key in (
            "review_count",
            "ai_error_count",
            "missing_address_count",
            "failed_geocode_count",
            "low_confidence_count",
            "review_required_count",
        ):
            value = metrics.get(key, 0)
            if isinstance(value, (int, float)) and value > 0:
                warnings.append(f"{stage_name} reported {key}={value}.")

    if context.manifest.get("warnings"):
        warnings.append("Run manifest contains stage warnings.")

    return (COMPLETED_WITH_WARNINGS if warnings else COMPLETED), warnings


def finalize_run(
    context: RunContext,
    *,
    allowed_skipped_stages: Iterable[str] = ("stage2", "manual_fixes"),
) -> tuple[str, list[str]]:
    context.reconcile_successful_stage_errors()
    status, warnings = determine_run_completion(
        context, allowed_skipped_stages=allowed_skipped_stages
    )
    active_errors = context.manifest.get("errors", [])
    if isinstance(active_errors, list) and active_errors:
        status = FAILED
        warnings = [*warnings, "Run manifest contains active fatal errors."]
    context.manifest["status"] = status
    context.manifest["canonical_for_import"] = False
    context.manifest["completion_warnings"] = warnings
    context.save()
    return status, warnings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create a versioned pipeline run directory.")
    parser.add_argument(
        "action",
        nargs="?",
        choices=(
            "create",
            "finalize",
            "reconcile",
            "summary",
            "skip",
            "approve",
            "unapprove",
            "approval-status",
        ),
        default="create",
    )
    parser.add_argument("--runs-root", type=Path, default=Path("data/runs"))
    parser.add_argument("--run-id")
    parser.add_argument("--run-dir", type=Path)
    parser.add_argument("--stage")
    parser.add_argument("--reason")
    parser.add_argument("--approved-by")
    parser.add_argument("--unapproved-by", "--changed-by", dest="unapproved_by")
    parser.add_argument("--note")
    parser.add_argument("--acknowledge-warnings", action="store_true")
    parser.add_argument("--confirm", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.action == "create":
        context = RunContext.create(args.runs_root, run_id=args.run_id)
        print(context.paths.root)
        return
    if args.run_dir is None:
        raise SystemExit(f"--run-dir is required for {args.action}")
    if args.action in {"approve", "unapprove", "approval-status"}:
        from pipeline.run_approval import (
            RunApprovalError,
            approve_run,
            evaluate_run_approval,
            print_approval_evaluation,
            unapprove_run,
        )

        evaluation = evaluate_run_approval(args.run_dir)
        print_approval_evaluation(evaluation)
        if args.action == "approval-status":
            return
        if args.action == "approve":
            if not args.approved_by or not args.approved_by.strip():
                raise SystemExit("--approved-by is required for approve")
            if evaluation.blocking_conditions:
                raise SystemExit("Run is not eligible for approval")
            if (
                evaluation.material_warning_conditions
                and not args.acknowledge_warnings
            ):
                raise SystemExit(
                    "Material warnings require --acknowledge-warnings"
                )
            if not args.confirm:
                print("Approval is eligible but unchanged; rerun with --confirm.")
                return
            try:
                approve_run(
                    args.run_dir,
                    approved_by=args.approved_by,
                    note=args.note,
                    acknowledge_warnings=args.acknowledge_warnings,
                    expected_manifest_file_fingerprint=(
                        evaluation.manifest_file_fingerprint
                    ),
                    expected_canonical_csv_fingerprint=(
                        evaluation.canonical_csv_fingerprint
                    ),
                )
            except RunApprovalError as error:
                raise SystemExit(str(error)) from error
            print("Run approved for Stage 4 import.")
            return
        if not args.unapproved_by or not args.unapproved_by.strip():
            raise SystemExit("--unapproved-by is required for unapprove")
        unapproval_note = args.reason or args.note
        if not unapproval_note or not unapproval_note.strip():
            raise SystemExit("--reason or --note is required for unapprove")
        try:
            unapprove_run(
                args.run_dir,
                unapproved_by=args.unapproved_by,
                reason=unapproval_note,
                expected_manifest_file_fingerprint=(
                    evaluation.manifest_file_fingerprint
                ),
            )
        except RunApprovalError as error:
            raise SystemExit(str(error)) from error
        print("Run unapproved; Stage 4 normal import is disabled.")
        return
    context = RunContext.resume(args.run_dir)
    if args.action == "skip":
        if not args.stage:
            raise SystemExit("--stage is required for skip")
        context.skip_stage(
            args.stage, reason=args.reason or "Skipped by pipeline operator."
        )
        print(json.dumps(context.manifest["stages"][args.stage], indent=2))
        return
    if args.action in {"finalize", "reconcile"}:
        finalize_run(context)
    print(json.dumps(context.manifest, indent=2))


if __name__ == "__main__":
    main()
