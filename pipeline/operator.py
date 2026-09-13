"""Local, review-gated operator for remote Stage 0-to-Stage 3 runs."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
from pathlib import Path, PureWindowsPath
import shutil
import subprocess
import sys
from typing import Any, Optional
from uuid import uuid4

from pipeline.operator_config import (
    OperatorConfig,
    OperatorConfigError,
    PROJECT_ROOT,
    load_config,
)
from pipeline.operator_summary import derive_current_metrics
from pipeline.review_workflow import (
    ReviewWorkflowError,
    apply_review_decisions,
    review_status,
    run_automated_review,
    staging_commands,
)
from pipeline.review_ui import (
    DEFAULT_HOST as REVIEW_UI_DEFAULT_HOST,
    DEFAULT_PORT as REVIEW_UI_DEFAULT_PORT,
    ReviewUIError,
    resolve_run_directory,
    serve_review_ui,
    validate_bind_host,
)
from pipeline.remote_executor import (
    CommandResult,
    RemoteExecutionError,
    RemoteExecutor,
    powershell_literal,
    redact_secrets,
)
from pipeline.run_context import (
    COMPLETED,
    COMPLETED_WITH_WARNINGS,
    FAILED,
    validate_run_id,
)


EXIT_SUCCESS = 0
EXIT_LOCAL_PREFLIGHT = 2
EXIT_REMOTE_PREFLIGHT = 3
EXIT_GIT_MISMATCH = 4
EXIT_REMOTE_EXECUTION = 5
EXIT_INVALID_RUN = 6
EXIT_REVIEW_REQUIRED = 7

SUCCESS_STATUSES = {COMPLETED, COMPLETED_WITH_WARNINGS}
REVIEW_ARTIFACTS = (
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


class OperatorError(RuntimeError):
    def __init__(
        self,
        message: str,
        exit_code: int,
        *,
        details: Optional[dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.exit_code = exit_code
        self.details = details or {}


@dataclass(frozen=True)
class LocalPreflight:
    repository: str
    branch: str
    commit: str
    tracked_dirty: bool
    dirty_override: bool
    executables: dict[str, str]
    review_root: str


def _git(arguments: list[str]) -> str:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OperatorError(f"Local Git check failed: {exc}", EXIT_LOCAL_PREFLIGHT) from exc
    return result.stdout.strip()


def local_preflight(config: OperatorConfig, *, allow_dirty: bool) -> LocalPreflight:
    if not (PROJECT_ROOT / ".git").exists() or not (
        PROJECT_ROOT / "pipeline" / "operator.py"
    ).is_file():
        raise OperatorError("Expected repository files are missing", EXIT_LOCAL_PREFLIGHT)
    executables: dict[str, str] = {}
    for name in ("git", "ssh", "scp"):
        executable = shutil.which(name)
        if not executable:
            raise OperatorError(f"Required local executable is missing: {name}", EXIT_LOCAL_PREFLIGHT)
        executables[name] = executable
    executables["python"] = sys.executable
    branch = _git(["branch", "--show-current"])
    commit = _git(["rev-parse", "HEAD"])
    dirty = bool(_git(["status", "--porcelain", "--untracked-files=no"]))
    if dirty and not allow_dirty:
        raise OperatorError(
            "Local tracked changes are uncommitted; use --allow-dirty-local only for development",
            EXIT_LOCAL_PREFLIGHT,
        )
    try:
        config.local.review_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OperatorError(f"Cannot create local review root: {exc}", EXIT_LOCAL_PREFLIGHT) from exc
    return LocalPreflight(
        repository=str(PROJECT_ROOT),
        branch=branch,
        commit=commit,
        tracked_dirty=dirty,
        dirty_override=dirty and allow_dirty,
        executables=executables,
        review_root=str(config.local.review_root),
    )


def _remote_script(config: OperatorConfig, arguments: list[str]) -> str:
    remote = config.remote
    python = remote.python_path
    if not PureWindowsPath(python).is_absolute():
        python = str(PureWindowsPath(remote.project_path) / python)
    encoded_arguments = " ".join(powershell_literal(value) for value in arguments)
    return (
        "$ErrorActionPreference = 'Stop'; try { "
        f"Set-Location -LiteralPath {powershell_literal(remote.project_path)}; "
        f"& {powershell_literal(python)} {encoded_arguments}; "
        "$RemoteExitCode = $LASTEXITCODE; "
        "if ($null -eq $RemoteExitCode) { $RemoteExitCode = 0 }; "
        "exit $RemoteExitCode "
        "} catch { [Console]::Error.WriteLine(($_ | Out-String)); exit 1 }"
    )


def _diagnostic_text(value: str, *, limit: int = 4000) -> str:
    sanitized = redact_secrets(value).strip()
    if len(sanitized) <= limit:
        return sanitized
    return sanitized[:limit] + "\n[diagnostic truncated]"


def _sanitize_json_value(value: Any, *, key: Optional[str] = None) -> Any:
    sensitive_key = key and any(
        token in key.casefold()
        for token in ("api_key", "password", "database_url", "authorization", "token", "secret")
    )
    if sensitive_key:
        return "[REDACTED]"
    if isinstance(value, dict):
        return {
            str(item_key): _sanitize_json_value(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_json_value(item) for item in value]
    if isinstance(value, str):
        return redact_secrets(value)
    return value


def _json_result(result: CommandResult, *, operation: str) -> dict[str, Any]:
    """Parse one final JSON object while retaining actionable failure context."""

    raw_stdout = result.stdout or ""
    stderr = redact_secrets(result.stderr or "")
    nonempty_lines = [line for line in raw_stdout.splitlines() if line.strip()]
    parsed: Optional[dict[str, Any]] = None
    output_state = "empty"
    if nonempty_lines:
        final_line = nonempty_lines[-1].lstrip("\ufeff \t")
        try:
            candidate = json.loads(final_line)
        except json.JSONDecodeError:
            output_state = "malformed_json"
        else:
            if isinstance(candidate, dict):
                parsed = _sanitize_json_value(candidate)
                output_state = "valid_json"
            else:
                output_state = "json_not_object"

    if result.returncode == 0 and parsed is not None:
        return parsed

    details: dict[str, Any] = {
        "operation": operation,
        "remote_exit_code": result.returncode,
        "output_state": output_state,
        "stdout": _diagnostic_text(raw_stdout) or None,
        "stderr": _diagnostic_text(stderr) or None,
        "ssh_password_prompt_detected": "[SSH_PASSWORD_PROMPT]" in stderr,
    }
    if parsed is not None:
        details["remote_error"] = parsed
    reason = (
        f"exit code {result.returncode}"
        if result.returncode != 0
        else output_state.replace("_", " ")
    )
    raise OperatorError(
        f"Remote {operation} failed ({reason}). Use --verbose for sanitized diagnostics.",
        EXIT_REMOTE_EXECUTION,
        details=details,
    )


def remote_preflight(
    executor: RemoteExecutor,
    config: OperatorConfig,
    local: LocalPreflight,
    *,
    require_services: bool = True,
) -> dict[str, Any]:
    script = _remote_script(
        config,
        [
            "-m",
            "pipeline.operator_remote",
            "preflight",
            "--ollama-endpoint",
            config.remote.ollama_endpoint,
            "--ollama-model",
            config.remote.ollama_model,
            "--minimum-free-gb",
            str(config.remote.minimum_free_gb),
            *([] if require_services else ["--skip-service-checks"]),
        ],
    )
    command_result = executor.run_powershell(script, check=False)
    result = _json_result(command_result, operation="preflight")
    if not result.get("ok"):
        raise OperatorError(_preflight_failure_message(result), EXIT_REMOTE_PREFLIGHT)
    if result.get("git_commit") != local.commit:
        raise OperatorError(
            f"Local/remote Git commit mismatch: {local.commit} != {result.get('git_commit')}",
            EXIT_GIT_MISMATCH,
        )
    return result


def _preflight_failure_message(result: dict[str, Any]) -> str:
    labels = {
        "repository": "remote repository/files",
        "python": "configured remote Python executable",
        "python_imports": "remote Python imports",
        "runs_root_writable": "remote run directory write access",
        "free_disk": "remote free disk space",
    }
    if result.get("service_checks_required", True):
        labels.update(
            {
                "ollama_reachable": "Ollama API",
                "ollama_model": "required Ollama model",
            }
        )
    failures = [label for key, label in labels.items() if result.get(key) is not True]
    if result.get("git_dirty") is True:
        failures.append("clean remote tracked working tree")
    if result.get("service_checks_required", True) and result.get("geoapify") != "configured":
        failures.append(f"Geoapify configuration ({result.get('geoapify', 'missing')})")
    return "Remote preflight failed: " + ", ".join(failures or ["unknown check"])


def inspect_remote_run(
    executor: RemoteExecutor, config: OperatorConfig, run_id: str
) -> dict[str, Any]:
    validate_run_id(run_id)
    result = executor.run_powershell(
        _remote_script(
            config,
            ["-m", "pipeline.operator_remote", "inspect", "--run-id", run_id],
        ),
        check=False,
    )
    snapshot = _json_result(result, operation="run inspection")
    if snapshot.get("ok") is False:
        raise OperatorError(snapshot.get("message", "Invalid remote run"), EXIT_INVALID_RUN)
    return snapshot


def reconcile_remote_run(
    executor: RemoteExecutor, config: OperatorConfig, run_id: str
) -> dict[str, Any]:
    result = executor.run_powershell(
        _remote_script(
            config,
            ["-m", "pipeline.operator_remote", "reconcile", "--run-id", run_id],
        ),
        check=False,
    )
    return _json_result(result, operation="manifest reconciliation")


def rebuild_remote_canonical(
    executor: RemoteExecutor, config: OperatorConfig, run_id: str
) -> dict[str, Any]:
    """Rebuild canonical artifacts from current remote CSVs without services."""

    result = executor.run_powershell(
        _remote_script(
            config,
            [
                "-m",
                "pipeline.operator_remote",
                "rebuild-canonical",
                "--run-id",
                validate_run_id(run_id),
            ],
        ),
        check=False,
    )
    rebuilt = _json_result(result, operation="canonical rebuild")
    if rebuilt.get("ok") is False:
        raise OperatorError(
            rebuilt.get("message", "Canonical rebuild failed"), EXIT_INVALID_RUN
        )
    return rebuilt


def execute_remote_review_command(
    executor: RemoteExecutor,
    config: OperatorConfig,
    *,
    command: str,
    run_id: str,
) -> dict[str, Any]:
    result = executor.run_powershell(
        _remote_script(
            config,
            [
                "-m",
                "pipeline.operator_remote",
                command,
                "--run-id",
                validate_run_id(run_id),
            ],
        ),
        check=False,
    )
    payload = _json_result(result, operation=command.replace("-", " "))
    if payload.get("ok") is False:
        raise OperatorError(payload.get("message", f"{command} failed"), EXIT_INVALID_RUN)
    return payload


def _local_review_run_dir(args: argparse.Namespace) -> Path:
    if args.run_dir is not None:
        root = args.run_dir.resolve()
    else:
        run_id = validate_run_id(args.run_id)
        authoritative = PROJECT_ROOT / "data" / "runs" / run_id
        copied = PROJECT_ROOT / "data" / "remote-runs" / run_id
        root = authoritative if authoritative.is_dir() else copied
    if not root.is_dir() or not (root / "manifest.json").is_file():
        raise OperatorError(f"Selected local run does not exist: {root}", EXIT_INVALID_RUN)
    if root.name != validate_run_id(args.run_id):
        raise OperatorError("--run-dir does not match --run-id", EXIT_INVALID_RUN)
    return root


def _execute_local_review(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    root = _local_review_run_dir(args)
    if args.command == "review-auto":
        result = run_automated_review(root)
    elif args.command == "review-status":
        result = review_status(root)
    elif args.command == "apply-review-decisions":
        result = apply_review_decisions(root)
        result = {**result, **result["review_summary"]}
    else:
        result = review_status(root)
        result["commands"] = (
            staging_commands(args.run_id) if result.get("ready_for_approval") else []
        )
    result["recommended_next_action"] = (
        "ready_for_approval" if result.get("ready_for_approval") else "review_required"
    )
    return (
        EXIT_SUCCESS if result.get("ready_for_approval") else EXIT_REVIEW_REQUIRED,
        result,
    )


def _local_review_bundle(args: argparse.Namespace) -> Path:
    run_id = validate_run_id(args.run_id)
    copied_root = (
        args.review_root or PROJECT_ROOT / "data" / "remote-runs"
    ).resolve()
    roots = (
        [copied_root]
        if args.remote_host
        else [PROJECT_ROOT / "data" / "runs", copied_root]
    )
    try:
        return resolve_run_directory(run_id, roots)
    except (ReviewUIError, ValueError) as exc:
        raise OperatorError(str(exc), EXIT_INVALID_RUN) from exc


def _execute_review_ui(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    root = _local_review_bundle(args)
    try:
        host = validate_bind_host(
            args.host,
            unsafe_development_bind=args.unsafe_development_bind,
        )
    except ReviewUIError as exc:
        raise OperatorError(str(exc), EXIT_INVALID_RUN) from exc
    url_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    url = f"http://{url_host}:{args.port}/"
    print(f"Review dashboard: {url}")
    try:
        result = serve_review_ui(
            root,
            host=host,
            port=args.port,
            reviewer=args.reviewer,
            read_only=args.read_only,
            open_browser=not args.no_open,
            unsafe_development_bind=args.unsafe_development_bind,
        )
    except (ReviewUIError, ReviewWorkflowError, ValueError) as exc:
        raise OperatorError(str(exc), EXIT_INVALID_RUN) from exc
    result["recommended_next_action"] = "review_ui_stopped"
    return EXIT_SUCCESS, result


def sync_remote_review_decisions(
    executor: RemoteExecutor,
    config: OperatorConfig,
    *,
    run_id: str,
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Merge local human decisions into the authoritative remote decision log."""

    selected = validate_run_id(run_id)
    try:
        local_root = resolve_run_directory(selected, [config.local.review_root])
    except (ReviewUIError, ValueError) as exc:
        raise OperatorError(str(exc), EXIT_INVALID_RUN) from exc
    decisions_path = local_root / "review" / "review-decisions.jsonl"
    summary_path = local_root / "review" / "review-auto-summary.json"
    if not decisions_path.is_file() or not summary_path.is_file():
        raise OperatorError("Local review bundle is incomplete", EXIT_INVALID_RUN)
    try:
        local_summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise OperatorError("Local review summary is invalid", EXIT_INVALID_RUN) from exc
    if not isinstance(local_summary, dict):
        raise OperatorError("Local review summary is invalid", EXIT_INVALID_RUN)
    remote_canonical = snapshot.get("artifacts", {}).get("stage3/canonical.csv", {})
    if local_summary.get("canonical_sha256") != remote_canonical.get("sha256"):
        raise OperatorError(
            "Local and remote canonical fingerprints differ; refresh artifacts before sync",
            EXIT_INVALID_RUN,
        )
    incoming_name = f".incoming-review-decisions-{uuid4().hex}.jsonl"
    remote_review_dir = (
        PureWindowsPath(config.remote.project_path)
        / "data"
        / "runs"
        / selected
        / "review"
    )
    remote_incoming = str(remote_review_dir / incoming_name)
    try:
        executor.copy_to(decisions_path, remote_incoming)
        result = executor.run_powershell(
            _remote_script(
                config,
                [
                    "-m",
                    "pipeline.operator_remote",
                    "review-merge-decisions",
                    "--run-id",
                    selected,
                    "--incoming",
                    incoming_name,
                ],
            ),
            check=False,
        )
        payload = _json_result(result, operation="review decision sync")
        if payload.get("ok") is False:
            raise OperatorError(
                payload.get("message", "Review decision sync failed"),
                EXIT_INVALID_RUN,
            )
        return payload
    finally:
        cleanup = (
            "Remove-Item -LiteralPath "
            f"{powershell_literal(remote_incoming)} "
            "-Force -ErrorAction SilentlyContinue"
        )
        executor.run_powershell(
            cleanup,
            check=False,
        )


def determine_resume_stage(
    snapshot: dict[str, Any], *, retry_ai_errors: bool = False
) -> Optional[str]:
    """Return the earliest necessary stage, or None for a terminal run."""

    manifest = snapshot.get("manifest")
    artifacts = snapshot.get("artifacts")
    if not isinstance(manifest, dict) or not isinstance(artifacts, dict):
        raise OperatorError("Run snapshot is invalid", EXIT_INVALID_RUN)
    stages = manifest.get("stages", {})
    if not isinstance(stages, dict):
        raise OperatorError("Run stages are invalid", EXIT_INVALID_RUN)

    required = (
        ("stage0", "stage0/listing_links.csv"),
        ("stage1", "stage1/website_ready.csv"),
        ("stage2", "stage2/enriched.csv"),
        ("manual_fixes", "stage2/reviewed.csv"),
        ("stage3", "stage3/geocoded.csv"),
        ("stage3_qc", "stage3/canonical.csv"),
    )
    for stage_name, output in required:
        stage = stages.get(stage_name, {})
        status = stage.get("status") if isinstance(stage, dict) else None
        artifact = artifacts.get(output, {})
        output_rows = stage.get("output_rows") if isinstance(stage, dict) else None
        output_ok = (
            artifact.get("exists") is True
            and isinstance(artifact.get("size"), int)
            and artifact["size"] > 0
            and isinstance(artifact.get("sha256"), str)
            and artifact.get("row_count") == output_rows
        )
        rows_known = isinstance(stage, dict) and stage.get("output_rows") is not None
        if stage_name == "stage2" and status in SUCCESS_STATUSES:
            metrics = stage.get("metrics", {})
            errors = metrics.get("ai_error_count", stage.get("error_count", 0))
            if errors and not retry_ai_errors:
                return None
        if status not in SUCCESS_STATUSES or not output_ok or not rows_known:
            return stage_name
    return None


def create_remote_run(executor: RemoteExecutor, config: OperatorConfig) -> str:
    result = executor.run_powershell(
        _remote_script(
            config,
            ["-m", "pipeline.run_context", "create", "--runs-root", r"data\runs"],
        )
    )
    output = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not output:
        raise OperatorError("Remote run creation returned no path", EXIT_REMOTE_EXECUTION)
    run_id = PureWindowsPath(output[-1]).name
    validate_run_id(run_id)
    return run_id


def run_remote_pipeline(
    executor: RemoteExecutor,
    config: OperatorConfig,
    *,
    run_id: str,
    start_at: str,
) -> None:
    run_dir = str(PureWindowsPath(config.remote.project_path) / "data" / "runs" / run_id)
    script_path = str(PureWindowsPath(config.remote.project_path) / "scripts" / "run_full_pipeline.ps1")
    arguments = [
        "-RunDir",
        run_dir,
        "-StartAtStage",
        start_at,
        "-PythonPath",
        config.remote.python_path,
        "-GeocodeCache",
        config.pipeline.geocode_cache,
        "-OllamaModel",
        config.remote.ollama_model,
        "-SkipLaterStages",
    ]
    encoded_arguments = " ".join(powershell_literal(value) for value in arguments)
    command = (
        f"Set-Location -LiteralPath {powershell_literal(config.remote.project_path)}; "
        f"& {powershell_literal(script_path)} {encoded_arguments}; "
        "exit $LASTEXITCODE"
    )
    executor.stream_powershell(command)


def copy_review_artifacts(
    executor: RemoteExecutor,
    config: OperatorConfig,
    snapshot: dict[str, Any],
    *,
    refresh: bool,
) -> Path:
    run_id = validate_run_id(str(snapshot["run_id"]))
    destination = config.local.review_root / run_id
    if destination.exists() and any(destination.iterdir()) and not refresh:
        raise OperatorError(
            f"Local review artifacts already exist: {destination}; use --refresh-artifacts",
            EXIT_INVALID_RUN,
        )
    destination.mkdir(parents=True, exist_ok=True)
    remote_root = PureWindowsPath(config.remote.project_path) / "data" / "runs" / run_id
    metadata = snapshot.get("artifacts", {})
    for relative in REVIEW_ARTIFACTS:
        expected = metadata.get(relative, {})
        if not expected.get("exists"):
            continue
        local_path = destination / Path(relative)
        executor.copy_from(str(remote_root / PureWindowsPath(relative)), local_path)
        if local_path.stat().st_size != expected.get("size"):
            raise OperatorError(f"Copied artifact size mismatch: {relative}", EXIT_REMOTE_EXECUTION)
        from pipeline.run_approval import sha256_file

        if sha256_file(local_path) != expected.get("sha256"):
            raise OperatorError(f"Copied artifact hash mismatch: {relative}", EXIT_REMOTE_EXECUTION)
    return destination


def ensure_review_destination_available(
    config: OperatorConfig, run_id: str, *, refresh: bool
) -> None:
    destination = config.local.review_root / validate_run_id(run_id)
    if destination.exists() and any(destination.iterdir()) and not refresh:
        raise OperatorError(
            f"Local review artifacts already exist: {destination}; use --refresh-artifacts",
            EXIT_INVALID_RUN,
        )


def build_summary(
    snapshot: dict[str, Any],
    *,
    remote_commit: str,
    local_review_directory: Optional[Path],
    preflight_override: bool,
) -> dict[str, Any]:
    manifest = snapshot["manifest"]
    approval = snapshot.get("approval_summary", {})
    current = snapshot.get("current_metrics", {})
    if local_review_directory is not None:
        copied_manifest = local_review_directory / "manifest.json"
        if copied_manifest.is_file():
            try:
                local_manifest = json.loads(copied_manifest.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                local_manifest = None
            if isinstance(local_manifest, dict):
                manifest = local_manifest
                current = derive_current_metrics(local_review_directory, local_manifest)
    stages = manifest.get("stages", {})
    stage2_metrics = stages.get("stage2", {}).get("metrics", {})
    manual_metrics = stages.get("manual_fixes", {}).get("metrics", {})
    manifest_errors = manifest.get("errors", [])
    active_errors = len(manifest_errors) if isinstance(manifest_errors, list) else 0
    history = manifest.get("error_history", [])
    historical_resolved_errors = (
        sum(
            isinstance(item, dict) and bool(item.get("resolved_at_utc"))
            for item in history
        )
        if isinstance(history, list)
        else 0
    )
    ai_errors = current.get(
        "ai_errors", stage2_metrics.get("ai_error_count", approval.get("ai_errors"))
    )
    ai_review_rows = current.get("ai_review_rows", approval.get("ai_review_rows"))
    manual_review_rows = current.get(
        "manual_review_rows",
        approval.get("unresolved_manual_review_rows", manual_metrics.get("review_count")),
    )
    geocode_failures = current.get(
        "geocode_failures", approval.get("geocode_failures")
    )
    geocode_review_rows = current.get(
        "geocode_review_rows", approval.get("geocode_review_rows")
    )
    stage2_status = stages.get("stage2", {}).get("status")
    stage3_status = stages.get("stage3", {}).get("status")
    if active_errors:
        if stage2_status == FAILED:
            recommendation = "resume_stage2"
        elif stage3_status == FAILED:
            recommendation = "resume_stage3"
        else:
            recommendation = "failed"
    elif manifest.get("status") not in SUCCESS_STATUSES:
        recommendation = "failed"
    elif snapshot.get("approval_blocking_conditions"):
        recommendation = "failed"
    elif any((ai_review_rows or 0, manual_review_rows or 0, geocode_review_rows or 0)):
        recommendation = "review_required"
    else:
        recommendation = "ready_for_approval"
    summary = {
        "run_id": snapshot["run_id"],
        "remote_commit": remote_commit,
        "run_status": manifest.get("status"),
        "discovered_listings": approval.get("discovered_listings"),
        "stage1_failures": approval.get("stage1_failures"),
        "ai_calls": stage2_metrics.get("ai_call_count"),
        "ai_errors": ai_errors,
        "ai_review_rows": ai_review_rows,
        "manual_review_rows": manual_review_rows,
        "missing_addresses": approval.get("missing_addresses"),
        "geocode_failures": geocode_failures,
        "low_confidence_geocodes": approval.get("low_confidence_geocodes"),
        "geocode_review_rows": geocode_review_rows,
        "missing_monthly_prices": approval.get("missing_monthly_price_rows"),
        "map_ready_rows": approval.get("map_ready_rows"),
        "not_map_ready_rows": approval.get("not_map_ready_rows"),
        "current_active_errors": active_errors,
        "historical_resolved_errors": historical_resolved_errors,
        "local_review_directory": str(local_review_directory) if local_review_directory else None,
        "dirty_local_override": preflight_override,
        "recommended_next_action": recommendation,
        "metric_sources": current.get("metric_sources", {}),
        "metric_discrepancies": current.get("metric_discrepancies", []),
    }
    return summary


def _write_summary(summary: dict[str, Any], directory: Optional[Path]) -> None:
    if directory is None:
        return
    path = directory / "operator-summary.json"
    path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _print_human(summary: dict[str, Any]) -> None:
    print(f"Run: {summary['run_id']} ({summary['run_status']})")
    print(
        "Reviews: "
        f"AI={summary['ai_review_rows'] or 0}, geocode={summary['geocode_review_rows'] or 0}; "
        f"active errors={summary['current_active_errors']}"
    )
    if summary["local_review_directory"]:
        print(f"Review artifacts: {summary['local_review_directory']}")
    if summary.get("metric_discrepancies"):
        print(
            f"Metric discrepancies: {len(summary['metric_discrepancies'])} "
            "(see operator-summary.json)"
        )
    print(f"Next action: {summary['recommended_next_action']}")


def _print_review_human(result: dict[str, Any]) -> None:
    print(f"Run: {result['run_id']}")
    counts = result.get("decision_counts", {})
    print(
        "Review decisions: "
        f"automatic={counts.get('auto_resolved', 0)}, "
        f"unknown={counts.get('accepted_as_unknown', 0)}, "
        f"human={counts.get('human_review_required', 0)}"
    )
    if result.get("local_review_directory"):
        print(f"Review artifacts: {result['local_review_directory']}")
    for command in result.get("commands", []):
        print(command)
    print(f"Next action: {result['recommended_next_action']}")


def _summary_exit_code(summary: dict[str, Any]) -> int:
    recommendation = summary.get("recommended_next_action")
    if recommendation == "ready_for_approval":
        return EXIT_SUCCESS
    if recommendation == "review_required":
        return EXIT_REVIEW_REQUIRED
    return EXIT_REMOTE_EXECUTION


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in (
        "run",
        "resume",
        "status",
        "preflight",
        "rebuild-canonical",
        "review-auto",
        "review-status",
        "apply-review-decisions",
        "review-next-steps",
        "review-ui",
        "review-sync-decisions",
    ):
        command = commands.add_parser(name)
        command.add_argument("--remote-host")
        command.add_argument("--config", type=Path)
        command.add_argument("--remote-project-path")
        command.add_argument("--remote-python-path")
        command.add_argument("--review-root", type=Path)
        command.add_argument("--allow-dirty-local", action="store_true")
        command.add_argument("--verbose", action="store_true")
        command.add_argument("--json", action="store_true")
        if name in {
            "resume",
            "status",
            "rebuild-canonical",
            "review-auto",
            "review-status",
            "apply-review-decisions",
            "review-next-steps",
            "review-ui",
            "review-sync-decisions",
        }:
            command.add_argument(
                "--run-id", required=name != "status"
            )
        if name in {
            "review-auto",
            "review-status",
            "apply-review-decisions",
            "review-next-steps",
        }:
            command.add_argument("--run-dir", type=Path)
        if name == "review-ui":
            command.add_argument("--host", default=REVIEW_UI_DEFAULT_HOST)
            command.add_argument("--port", type=int, default=REVIEW_UI_DEFAULT_PORT)
            command.add_argument("--no-open", action="store_true")
            command.add_argument("--read-only", action="store_true")
            command.add_argument("--reviewer")
            command.add_argument("--unsafe-development-bind", action="store_true")
        if name in {"run", "resume"}:
            command.add_argument("--refresh-artifacts", action="store_true")
            command.add_argument("--retry-ai-errors", action="store_true")
        if name == "resume":
            command.add_argument("--reconcile-only", action="store_true")
    return parser


def execute(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    review_commands = {
        "review-auto",
        "review-status",
        "apply-review-decisions",
        "review-next-steps",
    }
    if args.command == "review-ui":
        return _execute_review_ui(args)
    if args.command == "review-sync-decisions" and not args.remote_host:
        raise OperatorError(
            "review-sync-decisions requires --remote-host",
            EXIT_INVALID_RUN,
        )
    if args.command in review_commands and not args.remote_host:
        return _execute_local_review(args)
    config = load_config(
        config_path=args.config,
        host=args.remote_host,
        project_path=args.remote_project_path,
        python_path=args.remote_python_path,
        review_root=args.review_root,
    )
    local = local_preflight(config, allow_dirty=args.allow_dirty_local)
    log_path = config.local.review_root / "operator.log"
    executor = RemoteExecutor(
        config.remote,
        maximum_retries=config.pipeline.maximum_retries,
        retry_delay_seconds=config.pipeline.retry_delay_seconds,
        log_path=log_path,
        verbose=args.verbose,
    )
    remote = remote_preflight(
        executor,
        config,
        local,
        require_services=args.command in {"run", "preflight"},
    )
    if args.command == "preflight" or (args.command == "status" and not args.run_id):
        return EXIT_SUCCESS, {
            "ok": True,
            "local": asdict(local),
            "remote": remote,
            "recommended_next_action": "ready_for_run",
        }

    if args.command == "run":
        run_id = create_remote_run(executor, config)
        start_at = "stage0"
    else:
        run_id = validate_run_id(args.run_id)
        snapshot = inspect_remote_run(executor, config, run_id)
        if args.command == "review-sync-decisions":
            result = sync_remote_review_decisions(
                executor,
                config,
                run_id=run_id,
                snapshot=snapshot,
            )
            result["recommended_next_action"] = "apply_review_decisions"
            return EXIT_SUCCESS, result
        if args.command == "rebuild-canonical":
            rebuild = rebuild_remote_canonical(executor, config, run_id)
            snapshot = inspect_remote_run(executor, config, run_id)
            destination = copy_review_artifacts(
                executor,
                config,
                snapshot,
                refresh=True,
            )
            summary = build_summary(
                snapshot,
                remote_commit=remote["git_commit"],
                local_review_directory=destination,
                preflight_override=local.dirty_override,
            )
            summary["canonical_rebuild"] = rebuild
            _write_summary(summary, destination)
            return _summary_exit_code(summary), summary
        if args.command in review_commands:
            result = execute_remote_review_command(
                executor, config, command=args.command, run_id=run_id
            )
            if args.command in {"review-auto", "apply-review-decisions"}:
                snapshot = inspect_remote_run(executor, config, run_id)
                destination = copy_review_artifacts(
                    executor, config, snapshot, refresh=True
                )
                result["local_review_directory"] = str(destination)
            result["recommended_next_action"] = (
                "ready_for_approval"
                if result.get("ready_for_approval")
                else "review_required"
            )
            return (
                EXIT_SUCCESS
                if result.get("ready_for_approval")
                else EXIT_REVIEW_REQUIRED,
                result,
            )
        if args.command == "resume":
            reconcile_remote_run(executor, config, run_id)
            snapshot = inspect_remote_run(executor, config, run_id)
        start_at = determine_resume_stage(
            snapshot, retry_ai_errors=getattr(args, "retry_ai_errors", False)
        )
        if (
            args.command == "status"
            or getattr(args, "reconcile_only", False)
            or start_at is None
        ):
            destination: Optional[Path] = None
            if (
                args.command == "resume"
                and not getattr(args, "reconcile_only", False)
                and snapshot.get("artifacts", {})
                .get("stage3/canonical.csv", {})
                .get("exists")
            ):
                destination = copy_review_artifacts(
                    executor,
                    config,
                    snapshot,
                    refresh=getattr(args, "refresh_artifacts", False),
                )
            summary = build_summary(
                snapshot,
                remote_commit=remote["git_commit"],
                local_review_directory=destination,
                preflight_override=local.dirty_override,
            )
            _write_summary(summary, destination)
            return _summary_exit_code(summary), summary

    if args.command == "resume":
        remote = remote_preflight(
            executor, config, local, require_services=True
        )
    ensure_review_destination_available(
        config,
        run_id,
        refresh=getattr(args, "refresh_artifacts", False),
    )
    try:
        run_remote_pipeline(executor, config, run_id=run_id, start_at=start_at)
    except RemoteExecutionError as pipeline_error:
        try:
            failed_snapshot = inspect_remote_run(executor, config, run_id)
        except (RemoteExecutionError, OperatorError):
            raise pipeline_error
        failed_summary = build_summary(
            failed_snapshot,
            remote_commit=remote["git_commit"],
            local_review_directory=None,
            preflight_override=local.dirty_override,
        )
        failed_summary["operator_error"] = str(pipeline_error)
        return EXIT_REMOTE_EXECUTION, failed_summary
    snapshot = inspect_remote_run(executor, config, run_id)
    destination: Optional[Path] = None
    if snapshot.get("artifacts", {}).get("stage3/canonical.csv", {}).get("exists"):
        destination = copy_review_artifacts(
            executor,
            config,
            snapshot,
            refresh=getattr(args, "refresh_artifacts", False),
        )
    summary = build_summary(
        snapshot,
        remote_commit=remote["git_commit"],
        local_review_directory=destination,
        preflight_override=local.dirty_override,
    )
    _write_summary(summary, destination)
    return _summary_exit_code(summary), summary


def main() -> None:
    args = build_parser().parse_args()
    try:
        exit_code, result = execute(args)
    except OperatorConfigError as exc:
        exit_code, result = EXIT_LOCAL_PREFLIGHT, {
            "ok": False,
            "error": str(exc),
            "recommended_next_action": "fix_preflight",
        }
    except RemoteExecutionError as exc:
        exit_code, result = EXIT_REMOTE_EXECUTION, {
            "ok": False,
            "error": str(exc),
            "recommended_next_action": "failed",
        }
    except OperatorError as exc:
        exit_code, result = exc.exit_code, {
            "ok": False,
            "error": str(exc),
            "recommended_next_action": (
                "fix_preflight"
                if exc.exit_code in {EXIT_LOCAL_PREFLIGHT, EXIT_REMOTE_PREFLIGHT, EXIT_GIT_MISMATCH}
                else "failed"
            ),
        }
        if exc.details:
            result["diagnostics"] = exc.details
    if args.json:
        print(json.dumps(result, sort_keys=True))
    elif result.get("run_id"):
        if args.command.startswith("review") or args.command == "apply-review-decisions":
            _print_review_human(result)
        else:
            _print_human(result)
    elif result.get("ok"):
        print("Local and remote preflight passed.")
    else:
        print(f"Operator failed: {result['error']}", file=sys.stderr)
        if args.verbose and result.get("diagnostics"):
            diagnostics = result["diagnostics"]
            print(
                f"Operation: {diagnostics.get('operation')}; "
                f"remote exit code: {diagnostics.get('remote_exit_code')}; "
                f"output: {diagnostics.get('output_state')}",
                file=sys.stderr,
            )
            if diagnostics.get("stderr"):
                print(f"Remote stderr:\n{diagnostics['stderr']}", file=sys.stderr)
            if diagnostics.get("stdout"):
                print(f"Remote stdout:\n{diagnostics['stdout']}", file=sys.stderr)
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
