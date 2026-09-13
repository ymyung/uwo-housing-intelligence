from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path
import subprocess

import pytest

from pipeline import operator, operator_remote
from pipeline.operator_config import (
    LocalConfig,
    OperatorConfig,
    OperatorConfigError,
    PipelineConfig,
    RemoteConfig,
    load_config,
)
from pipeline.remote_executor import (
    CommandResult,
    RemoteExecutionError,
    RemoteExecutor,
    redact_secrets,
)


def config(tmp_path: Path, **remote_overrides) -> OperatorConfig:
    remote = RemoteConfig(host="fixture-host", project_path=r"C:\fixture repo")
    remote = RemoteConfig(**{**remote.__dict__, **remote_overrides})
    return OperatorConfig(remote, LocalConfig(tmp_path / "reviews"), PipelineConfig())


def write_dict_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def test_local_clean_preflight_and_dirty_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    values = {
        ("branch", "--show-current"): "main",
        ("rev-parse", "HEAD"): "abc123",
        ("status", "--porcelain", "--untracked-files=no"): "",
    }
    monkeypatch.setattr(operator, "_git", lambda arguments: values[tuple(arguments)])
    monkeypatch.setattr(operator.shutil, "which", lambda name: f"C:/{name}.exe")
    result = operator.local_preflight(config(tmp_path), allow_dirty=False)
    assert result.commit == "abc123"
    assert not result.tracked_dirty
    assert Path(result.review_root).is_dir()

    values[("status", "--porcelain", "--untracked-files=no")] = " M tracked.py"
    with pytest.raises(operator.OperatorError, match="uncommitted"):
        operator.local_preflight(config(tmp_path), allow_dirty=False)
    overridden = operator.local_preflight(config(tmp_path), allow_dirty=True)
    assert overridden.dirty_override


def test_local_preflight_reports_missing_ssh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(operator.shutil, "which", lambda name: None if name == "ssh" else name)
    with pytest.raises(operator.OperatorError, match="ssh"):
        operator.local_preflight(config(tmp_path), allow_dirty=False)


def test_config_precedence_and_unsafe_host_rejection(tmp_path: Path) -> None:
    path = tmp_path / "operator.toml"
    path.write_text(
        '[remote]\nhost="from-file"\nproject_path="C:\\\\repo"\n'
        '[local]\nreview_root="reviews"\n',
        encoding="utf-8",
    )
    loaded = load_config(config_path=path, host="from-cli", review_root=tmp_path / "out")
    assert loaded.remote.host == "from-cli"
    assert loaded.local.review_root == (tmp_path / "out").resolve()
    with pytest.raises(OperatorConfigError, match="plain SSH"):
        load_config(config_path=path, host="-oProxyCommand=bad")


def test_remote_executor_uses_safe_arguments_windows_powershell_and_retry_limit(
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    def runner(arguments, **kwargs):
        calls.append(arguments)
        return subprocess.CompletedProcess(arguments, 255, "", "connection reset")

    executor = RemoteExecutor(
        config(tmp_path).remote,
        maximum_retries=2,
        retry_delay_seconds=0,
        runner=runner,
    )
    with pytest.raises(RemoteExecutionError, match="connection reset"):
        executor.run_powershell("Get-Location")
    assert len(calls) == 3
    assert calls[0][:3] == ["ssh", "fixture-host", "powershell.exe"]
    assert "pwsh" not in calls[0]


def test_remote_executor_copy_to_uses_explicit_scp_arguments(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    source = tmp_path / "review decisions.jsonl"
    source.write_text("{}\n", encoding="utf-8")

    def runner(arguments, **kwargs):
        calls.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, "", "")

    executor = RemoteExecutor(
        RemoteConfig(host="fixture-host"),
        runner=runner,
        maximum_retries=0,
    )
    executor.copy_to(source, r"C:\fixture repo\review\incoming.jsonl")
    assert calls == [
        [
            "scp",
            str(source),
            "fixture-host:C:/fixture repo/review/incoming.jsonl",
        ]
    ]


def test_remote_connection_failure_is_not_retried_forever(tmp_path: Path) -> None:
    def runner(arguments, **kwargs):
        return subprocess.CompletedProcess(arguments, 255, "", "connection refused")

    executor = RemoteExecutor(config(tmp_path).remote, maximum_retries=0, runner=runner)
    with pytest.raises(RemoteExecutionError, match="connection refused"):
        executor.run_powershell("$true")


def test_missing_geoapify_configuration_is_not_retried(tmp_path: Path) -> None:
    calls = 0

    def runner(arguments, **kwargs):
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(
            arguments,
            255,
            "",
            "Missing GEOAPIFY_API_KEY environment variable",
        )

    executor = RemoteExecutor(
        config(tmp_path).remote,
        maximum_retries=3,
        retry_delay_seconds=0,
        runner=runner,
    )
    with pytest.raises(RemoteExecutionError, match="GEOAPIFY"):
        executor.run_powershell("$true")
    assert calls == 1


def test_streamed_missing_geoapify_failure_is_not_retried(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = 0

    class Process:
        stdout = io.StringIO("Missing GEOAPIFY_API_KEY environment variable\n")

        @staticmethod
        def wait() -> int:
            return 255

    def popen_factory(arguments, **kwargs):
        nonlocal calls
        calls += 1
        return Process()

    executor = RemoteExecutor(
        config(tmp_path).remote,
        maximum_retries=3,
        retry_delay_seconds=0,
        popen_factory=popen_factory,
    )
    with pytest.raises(RemoteExecutionError, match="GEOAPIFY"):
        executor.stream_powershell("$true")
    assert calls == 1
    assert "GEOAPIFY_API_KEY" in capsys.readouterr().err


def test_remote_executor_can_return_sanitized_nonzero_result_for_json_diagnostics(
    tmp_path: Path,
) -> None:
    def runner(arguments, **kwargs):
        return subprocess.CompletedProcess(
            arguments,
            1,
            "debug API_TOKEN=top-secret",
            "Python failed",
        )

    executor = RemoteExecutor(config(tmp_path).remote, runner=runner)
    result = executor.run_powershell("$true", check=False)
    assert result.returncode == 1
    assert "top-secret" not in result.stdout
    assert "[REDACTED]" in result.stdout


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"ollama_reachable": False}, "Ollama API"),
        ({"ollama_model": False}, "required Ollama model"),
        ({"geoapify": "missing"}, "Geoapify configuration (missing)"),
        ({"python_imports": False}, "remote Python imports"),
    ],
)
def test_remote_preflight_failure_messages(changes: dict, expected: str) -> None:
    result = {
        "repository": True,
        "python_imports": True,
        "runs_root_writable": True,
        "free_disk": True,
        "ollama_reachable": True,
        "ollama_model": True,
        "geoapify": "configured",
    }
    result.update(changes)
    assert expected in operator._preflight_failure_message(result)
    assert "API_KEY" not in operator._preflight_failure_message(result)


def test_remote_preflight_rejects_commit_mismatch(tmp_path: Path) -> None:
    class Executor:
        def run_powershell(self, script, *, check=True):
            payload = {
                "ok": True,
                "git_commit": "remote",
                "geoapify": "configured",
            }
            return CommandResult(0, json.dumps(payload), "", 1)

    local = operator.LocalPreflight("repo", "main", "local", False, False, {}, "out")
    with pytest.raises(operator.OperatorError, match="mismatch") as error:
        operator.remote_preflight(Executor(), config(tmp_path), local)
    assert error.value.exit_code == operator.EXIT_GIT_MISMATCH


def test_remote_inspection_reads_legacy_null_error_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = "legacy-readable"
    run_dir = tmp_path / "data" / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "status": "failed",
                "stages": {},
                "errors": [],
                "error_history": None,
                "canonical_for_import": False,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(operator_remote, "PROJECT_ROOT", tmp_path)
    result = operator_remote.inspect_run(run_id)
    assert result["active_error_count"] == 0
    assert result["historical_resolved_errors"] == 0


def test_bookkeeping_preflight_skips_ollama_and_geoapify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(operator_remote, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(operator_remote, "_git", lambda arguments: "commit")
    monkeypatch.setattr(
        operator_remote.requests,
        "get",
        lambda *args, **kwargs: pytest.fail("Ollama contacted"),
    )
    result = operator_remote.remote_preflight(
        ollama_endpoint="http://127.0.0.1:11434",
        ollama_model="fixture-model",
        minimum_free_gb=0,
        require_services=False,
    )
    assert result["service_checks_required"] is False
    assert result["geoapify"] == "not_checked"
    assert result["ollama_reachable"] is None


@pytest.mark.parametrize(
    ("result", "state", "diagnostic"),
    [
        (CommandResult(0, "", "", 1), "empty", None),
        (
            CommandResult(1, "", "Traceback: ModuleNotFoundError: pandas", 1),
            "empty",
            "ModuleNotFoundError",
        ),
        (CommandResult(7, "", "remote Python failed", 1), "empty", "exit code 7"),
        (CommandResult(0, "{not-json}\n", "", 1), "malformed_json", "malformed"),
    ],
)
def test_expected_json_failures_retain_actionable_diagnostics(
    result: CommandResult, state: str, diagnostic: str | None
) -> None:
    with pytest.raises(operator.OperatorError) as caught:
        operator._json_result(result, operation="fixture preflight")
    error = caught.value
    assert error.details["operation"] == "fixture preflight"
    assert error.details["remote_exit_code"] == result.returncode
    assert error.details["output_state"] == state
    if diagnostic:
        assert diagnostic in (str(error) + json.dumps(error.details))


@pytest.mark.parametrize(
    "stdout",
    [
        'PowerShell informational text\n  {"ok": true, "value": 3}\n',
        '\ufeff{"ok": true, "value": 3}\n',
        '\n\t  {"ok": true, "value": 3}\n',
    ],
)
def test_final_json_object_tolerates_bom_whitespace_and_information(stdout: str) -> None:
    assert operator._json_result(
        CommandResult(0, stdout, "", 1), operation="fixture"
    ) == {"ok": True, "value": 3}


def test_unrelated_text_after_json_is_not_silently_accepted() -> None:
    result = CommandResult(0, '{"ok": true}\nunrelated trailing output\n', "", 1)
    with pytest.raises(operator.OperatorError) as caught:
        operator._json_result(result, operation="fixture")
    assert caught.value.details["output_state"] == "malformed_json"


def test_password_prompt_is_diagnostic_not_json() -> None:
    result = CommandResult(255, "", "user@host's password:", 3)
    with pytest.raises(operator.OperatorError) as caught:
        operator._json_result(result, operation="preflight")
    assert caught.value.details["ssh_password_prompt_detected"] is True
    assert caught.value.details["stderr"] == "[SSH_PASSWORD_PROMPT]"


def test_nonzero_valid_remote_error_json_is_not_success() -> None:
    result = CommandResult(
        1,
        '{"ok": false, "error": "ModuleNotFoundError", "message": "missing pandas"}',
        "",
        1,
    )
    with pytest.raises(operator.OperatorError) as caught:
        operator._json_result(result, operation="preflight")
    assert caught.value.details["output_state"] == "valid_json"
    assert caught.value.details["remote_error"]["error"] == "ModuleNotFoundError"


def test_valid_json_secret_fields_are_redacted_without_breaking_json() -> None:
    result = CommandResult(
        0,
        json.dumps(
            {
                "ok": True,
                "GEOAPIFY_API_KEY": "top-secret",
                "nested": {"authorization": "Bearer auth-secret"},
            }
        ),
        "",
        1,
    )
    parsed = operator._json_result(result, operation="preflight")
    assert parsed["ok"] is True
    assert parsed["GEOAPIFY_API_KEY"] == "[REDACTED]"
    assert parsed["nested"]["authorization"] == "[REDACTED]"
    assert "top-secret" not in json.dumps(parsed)


def test_missing_configured_python_is_surfaced(tmp_path: Path) -> None:
    class Executor:
        def run_powershell(self, script, *, check=True):
            return CommandResult(
                1,
                "",
                "The term 'C:\\fixture repo\\missing python.exe' is not recognized",
                1,
            )

    local = operator.LocalPreflight("repo", "main", "same", False, False, {}, "out")
    with pytest.raises(operator.OperatorError) as caught:
        operator.remote_preflight(
            Executor(), config(tmp_path, python_path=r"missing python.exe"), local
        )
    assert "preflight" in str(caught.value)
    assert "not recognized" in caught.value.details["stderr"]


def test_json_mode_emits_structured_error_for_invalid_remote_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    error = operator.OperatorError(
        "Remote preflight failed (malformed json).",
        operator.EXIT_REMOTE_EXECUTION,
        details={
            "operation": "preflight",
            "remote_exit_code": 1,
            "output_state": "malformed_json",
            "stdout": "diagnostic",
            "stderr": None,
        },
    )
    monkeypatch.setattr(operator, "execute", lambda args: (_ for _ in ()).throw(error))
    monkeypatch.setattr(
        operator.sys,
        "argv",
        ["operator", "preflight", "--remote-host", "fixture", "--json"],
    )
    with pytest.raises(SystemExit) as stopped:
        operator.main()
    payload = json.loads(capsys.readouterr().out)
    assert stopped.value.code == operator.EXIT_REMOTE_EXECUTION
    assert payload["diagnostics"]["remote_exit_code"] == 1
    assert payload["diagnostics"]["output_state"] == "malformed_json"


def test_verbose_mode_prints_sanitized_remote_diagnostics(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    error = operator.OperatorError(
        "Remote preflight failed (exit code 1).",
        operator.EXIT_REMOTE_EXECUTION,
        details={
            "operation": "preflight",
            "remote_exit_code": 1,
            "output_state": "empty",
            "stdout": None,
            "stderr": "ModuleNotFoundError: pandas",
        },
    )
    monkeypatch.setattr(operator, "execute", lambda args: (_ for _ in ()).throw(error))
    monkeypatch.setattr(
        operator.sys,
        "argv",
        ["operator", "preflight", "--remote-host", "fixture", "--verbose"],
    )
    with pytest.raises(SystemExit):
        operator.main()
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "remote exit code: 1" in captured.err
    assert "ModuleNotFoundError: pandas" in captured.err


def snapshot(statuses: dict[str, str], *, ai_errors: int = 0) -> dict:
    outputs = {
        "stage0": "stage0/listing_links.csv",
        "stage1": "stage1/website_ready.csv",
        "stage2": "stage2/enriched.csv",
        "manual_fixes": "stage2/reviewed.csv",
        "stage3": "stage3/geocoded.csv",
        "stage3_qc": "stage3/canonical.csv",
    }
    stages = {}
    artifacts = {}
    for name, output in outputs.items():
        status = statuses.get(name)
        if status is not None:
            stages[name] = {"status": status, "output_rows": 2, "metrics": {}}
            artifacts[output] = {
                "exists": status in operator.SUCCESS_STATUSES,
                "size": 10,
                "sha256": "a" * 64,
                "row_count": 2,
            }
    if "stage2" in stages:
        stages["stage2"]["metrics"]["ai_error_count"] = ai_errors
    return {"manifest": {"stages": stages}, "artifacts": artifacts}


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ({}, "stage0"),
        ({"stage0": "completed"}, "stage1"),
        (
            {"stage0": "completed", "stage1": "completed", "stage2": "failed"},
            "stage2",
        ),
        (
            {
                "stage0": "completed",
                "stage1": "completed",
                "stage2": "completed",
                "manual_fixes": "completed",
                "stage3": "failed",
            },
            "stage3",
        ),
        (
            {
                "stage0": "completed",
                "stage1": "completed",
                "stage2": "completed",
                "manual_fixes": "completed",
                "stage3": "completed",
            },
            "stage3_qc",
        ),
    ],
)
def test_resume_selects_only_earliest_required_stage(statuses, expected) -> None:
    assert operator.determine_resume_stage(snapshot(statuses)) == expected


def test_completed_run_does_not_rerun_and_ai_errors_require_explicit_retry() -> None:
    statuses = {name: "completed" for name in (
        "stage0", "stage1", "stage2", "manual_fixes", "stage3", "stage3_qc"
    )}
    assert operator.determine_resume_stage(snapshot(statuses)) is None
    partial = {"stage0": "completed", "stage1": "completed", "stage2": "completed"}
    with_errors = snapshot(partial, ai_errors=1)
    assert operator.determine_resume_stage(with_errors) is None
    assert operator.determine_resume_stage(with_errors, retry_ai_errors=True) == "manual_fixes"


def test_resume_requires_explicit_run_id() -> None:
    parser = operator.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["resume", "--remote-host", "fixture-host"])


def test_new_run_creation_extracts_windows_run_id(tmp_path: Path) -> None:
    class Executor:
        def run_powershell(self, script):
            return type("Result", (), {"stdout": "C:\\repo\\data\\runs\\run-123\n"})()

    assert operator.create_remote_run(Executor(), config(tmp_path)) == "run-123"


def test_review_artifacts_copy_hash_verify_and_refuse_overwrite(tmp_path: Path) -> None:
    content = b"a,b\n1,2\n"
    digest = hashlib.sha256(content).hexdigest()
    snap = {
        "run_id": "copy-run",
        "artifacts": {
            relative: {"exists": relative == "manifest.json", "size": len(content), "sha256": digest}
            for relative in operator.REVIEW_ARTIFACTS
        },
    }

    class Executor:
        def copy_from(self, remote_path, local_path):
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_bytes(content)

    destination = operator.copy_review_artifacts(
        Executor(), config(tmp_path), snap, refresh=False
    )
    assert (destination / "manifest.json").read_bytes() == content
    with pytest.raises(operator.OperatorError, match="already exist"):
        operator.copy_review_artifacts(Executor(), config(tmp_path), snap, refresh=False)


def test_summary_shape_and_recommendations(tmp_path: Path) -> None:
    snap = {
        "run_id": "summary-run",
        "manifest": {
            "status": "completed_with_warnings",
            "errors": [],
            "error_history": [{"resolved_at_utc": "2026-08-01T00:00:00Z"}],
            "stages": {
                "stage2": {"status": "completed", "metrics": {"ai_call_count": 12}},
                "stage3": {"status": "completed"},
                "manual_fixes": {"metrics": {"reviewed_count": 2}},
            },
        },
        "approval_summary": {"ai_review_rows": 2, "geocode_review_rows": 1},
        "approval_blocking_conditions": [],
        "active_error_count": 0,
        "historical_resolved_errors": 1,
    }
    result = operator.build_summary(
        snap,
        remote_commit="abc",
        local_review_directory=tmp_path,
        preflight_override=True,
    )
    assert result["recommended_next_action"] == "review_required"
    assert result["ai_calls"] == 12
    assert result["historical_resolved_errors"] == 1
    assert result["dirty_local_override"] is True


def test_summary_uses_current_stage_artifacts_and_reports_stale_canonical(
    tmp_path: Path,
) -> None:
    review_root = tmp_path / "copied-run"
    manifest = {
        "run_id": "copied-run",
        "status": "completed_with_warnings",
        "stages": {
            "stage2": {
                "status": "completed_with_warnings",
                "metrics": {"ai_error_count": 0, "review_count": 18},
            },
            "manual_fixes": {
                "status": "completed_with_warnings",
                "metrics": {"review_count": 18},
            },
            "stage3": {
                "status": "completed_with_warnings",
                "metrics": {"failed_geocode_count": 0},
            },
            "stage3_qc": {
                "status": "completed_with_warnings",
                "metrics": {"review_required_count": 2},
            },
        },
        "errors": [],
    }
    review_root.mkdir()
    (review_root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    write_dict_csv(
        review_root / "stage2" / "review_queue.csv",
        ["listing_id"],
        [{"listing_id": str(index)} for index in range(18)],
    )
    write_dict_csv(
        review_root / "stage2" / "reviewed.csv",
        ["listing_id", "ai_error", "needs_manual_review", "manual_reviewed"],
        [
            {
                "listing_id": str(index),
                "ai_error": "",
                "needs_manual_review": "True",
                "manual_reviewed": "True" if index < 6 else "False",
            }
            for index in range(18)
        ],
    )
    write_dict_csv(
        review_root / "stage3" / "geocode_review.csv",
        ["listing_id"],
        [{"listing_id": "1"}, {"listing_id": "2"}],
    )
    write_dict_csv(
        review_root / "stage3" / "canonical.csv",
        ["listing_id", "ai_error", "needs_manual_review"],
        [
            {"listing_id": "1", "ai_error": "old connection error", "needs_manual_review": "True"},
            {"listing_id": "2", "ai_error": "old connection error", "needs_manual_review": "False"},
        ],
    )
    snap = {
        "run_id": "copied-run",
        "manifest": manifest,
        "approval_summary": {
            "ai_errors": 816,
            "ai_review_rows": 13,
            "unresolved_manual_review_rows": 12,
            "geocode_failures": 2,
            "geocode_review_rows": 94,
        },
        "approval_blocking_conditions": [],
        "active_error_count": 0,
        "historical_resolved_errors": 3,
    }

    result = operator.build_summary(
        snap,
        remote_commit="commit",
        local_review_directory=review_root,
        preflight_override=False,
    )
    assert result["ai_errors"] == 0
    assert result["ai_review_rows"] == 18
    assert result["manual_review_rows"] == 12
    assert result["geocode_failures"] == 0
    assert result["geocode_review_rows"] == 2
    assert result["run_status"] == "completed_with_warnings"
    assert result["recommended_next_action"] == "review_required"
    assert any(
        item.get("artifact") == "stage3/canonical.csv"
        and item["metric"] == "ai_errors"
        for item in result["metric_discrepancies"]
    )


def test_completed_clean_summary_is_ready_for_approval() -> None:
    snap = {
        "run_id": "clean-run",
        "manifest": {
            "status": "completed",
            "stages": {
                "stage2": {"status": "completed", "metrics": {}},
                "stage3": {"status": "completed", "metrics": {}},
            },
        },
        "current_metrics": {
            "ai_errors": 0,
            "ai_review_rows": 0,
            "manual_review_rows": 0,
            "geocode_failures": 0,
            "geocode_review_rows": 0,
        },
        "approval_summary": {},
        "approval_blocking_conditions": [],
        "active_error_count": 0,
        "historical_resolved_errors": 3,
    }
    result = operator.build_summary(
        snap,
        remote_commit="commit",
        local_review_directory=None,
        preflight_override=False,
    )
    assert result["recommended_next_action"] == "ready_for_approval"


def completed_operator_snapshot(run_id: str = "completed-run") -> dict:
    statuses = {
        name: "completed"
        for name in (
            "stage0",
            "stage1",
            "stage2",
            "manual_fixes",
            "stage3",
            "stage3_qc",
        )
    }
    value = snapshot(statuses)
    value.update(
        {
            "run_id": run_id,
            "approval_summary": {},
            "approval_blocking_conditions": [],
            "active_error_count": 0,
            "historical_resolved_errors": 3,
            "current_metrics": {
                "ai_errors": 0,
                "ai_review_rows": 0,
                "manual_review_rows": 0,
                "geocode_failures": 0,
                "geocode_review_rows": 0,
            },
        }
    )
    value["manifest"]["status"] = "completed"
    return value


def test_reconcile_only_repairs_bookkeeping_without_expensive_stages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path)
    events: list[str] = []
    current = completed_operator_snapshot("repair-run")
    monkeypatch.setattr(operator, "load_config", lambda **kwargs: cfg)
    monkeypatch.setattr(
        operator,
        "local_preflight",
        lambda *args, **kwargs: operator.LocalPreflight(
            "repo", "main", "commit", False, False, {}, str(tmp_path)
        ),
    )
    monkeypatch.setattr(operator, "RemoteExecutor", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        operator,
        "remote_preflight",
        lambda *args, **kwargs: {"git_commit": "commit", "ok": True},
    )
    monkeypatch.setattr(operator, "inspect_remote_run", lambda *args: current)
    monkeypatch.setattr(
        operator,
        "reconcile_remote_run",
        lambda *args: events.append("reconcile") or {"ok": True},
    )
    monkeypatch.setattr(
        operator,
        "run_remote_pipeline",
        lambda *args, **kwargs: pytest.fail("expensive stage executed"),
    )
    args = operator.build_parser().parse_args(
        [
            "resume",
            "--remote-host",
            "fixture-host",
            "--run-id",
            "repair-run",
            "--reconcile-only",
        ]
    )
    exit_code, result = operator.execute(args)
    assert events == ["reconcile"]
    assert exit_code == operator.EXIT_SUCCESS
    assert result["recommended_next_action"] == "ready_for_approval"


def test_rebuild_canonical_skips_services_and_processing_then_refreshes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path)
    events: list[object] = []
    current = completed_operator_snapshot("rebuild-run")
    current["manifest"]["status"] = "completed_with_warnings"
    current["current_metrics"]["ai_review_rows"] = 1
    monkeypatch.setattr(operator, "load_config", lambda **kwargs: cfg)
    monkeypatch.setattr(
        operator,
        "local_preflight",
        lambda *args, **kwargs: operator.LocalPreflight(
            "repo", "main", "commit", False, False, {}, str(tmp_path)
        ),
    )
    monkeypatch.setattr(operator, "RemoteExecutor", lambda *args, **kwargs: object())

    def preflight(*args, **kwargs):
        events.append(("preflight_services", kwargs["require_services"]))
        return {"git_commit": "commit", "ok": True}

    monkeypatch.setattr(operator, "remote_preflight", preflight)
    monkeypatch.setattr(operator, "inspect_remote_run", lambda *args: current)
    monkeypatch.setattr(
        operator,
        "rebuild_remote_canonical",
        lambda *args: events.append("rebuild") or {"ok": True, "idempotent": False},
    )
    monkeypatch.setattr(
        operator,
        "copy_review_artifacts",
        lambda *args, **kwargs: events.append(("refresh", kwargs["refresh"])) or tmp_path,
    )
    monkeypatch.setattr(
        operator,
        "run_remote_pipeline",
        lambda *args, **kwargs: pytest.fail("Stage 0-3 processing executed"),
    )

    args = operator.build_parser().parse_args(
        ["rebuild-canonical", "--remote-host", "fixture", "--run-id", "rebuild-run"]
    )
    exit_code, summary = operator.execute(args)

    assert exit_code == operator.EXIT_REVIEW_REQUIRED
    assert events == [("preflight_services", False), "rebuild", ("refresh", True)]
    assert summary["canonical_rebuild"]["ok"] is True
    assert summary["recommended_next_action"] == "review_required"


def test_local_review_command_does_not_require_ssh_or_service_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "local-review"
    run_dir.mkdir()
    (run_dir / "manifest.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        operator,
        "load_config",
        lambda **kwargs: pytest.fail("remote configuration loaded"),
    )
    monkeypatch.setattr(
        operator,
        "local_preflight",
        lambda *args, **kwargs: pytest.fail("SSH preflight executed"),
    )
    monkeypatch.setattr(
        operator,
        "run_automated_review",
        lambda root: {
            "run_id": "local-review",
            "ready_for_approval": False,
            "decision_counts": {"human_review_required": 2},
        },
    )
    args = operator.build_parser().parse_args(
        [
            "review-auto",
            "--run-id",
            "local-review",
            "--run-dir",
            str(run_dir),
        ]
    )
    exit_code, result = operator.execute(args)
    assert exit_code == operator.EXIT_REVIEW_REQUIRED
    assert result["recommended_next_action"] == "review_required"


def test_remote_review_command_skips_services_and_refreshes_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path)
    events: list[object] = []
    current = completed_operator_snapshot("review-run")
    monkeypatch.setattr(operator, "load_config", lambda **kwargs: cfg)
    monkeypatch.setattr(
        operator,
        "local_preflight",
        lambda *args, **kwargs: operator.LocalPreflight(
            "repo", "main", "commit", False, False, {}, str(tmp_path)
        ),
    )
    monkeypatch.setattr(operator, "RemoteExecutor", lambda *args, **kwargs: object())

    def preflight(*args, **kwargs):
        events.append(("services", kwargs["require_services"]))
        return {"git_commit": "commit", "ok": True}

    monkeypatch.setattr(operator, "remote_preflight", preflight)
    monkeypatch.setattr(operator, "inspect_remote_run", lambda *args: current)
    monkeypatch.setattr(
        operator,
        "execute_remote_review_command",
        lambda *args, **kwargs: events.append(kwargs["command"])
        or {
            "run_id": "review-run",
            "ready_for_approval": False,
            "decision_counts": {"human_review_required": 1},
        },
    )
    monkeypatch.setattr(
        operator,
        "copy_review_artifacts",
        lambda *args, **kwargs: events.append(("refresh", kwargs["refresh"]))
        or tmp_path,
    )
    monkeypatch.setattr(
        operator,
        "run_remote_pipeline",
        lambda *args, **kwargs: pytest.fail("pipeline processing executed"),
    )
    args = operator.build_parser().parse_args(
        ["review-auto", "--remote-host", "fixture", "--run-id", "review-run"]
    )
    exit_code, result = operator.execute(args)
    assert exit_code == operator.EXIT_REVIEW_REQUIRED
    assert events == [("services", False), "review-auto", ("refresh", True)]
    assert result["recommended_next_action"] == "review_required"


def test_refreshing_completed_artifacts_does_not_execute_stages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path)
    current = completed_operator_snapshot("refresh-run")
    destination = cfg.local.review_root / "refresh-run"
    monkeypatch.setattr(operator, "load_config", lambda **kwargs: cfg)
    monkeypatch.setattr(
        operator,
        "local_preflight",
        lambda *args, **kwargs: operator.LocalPreflight(
            "repo", "main", "commit", False, False, {}, str(tmp_path)
        ),
    )
    monkeypatch.setattr(operator, "RemoteExecutor", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        operator,
        "remote_preflight",
        lambda *args, **kwargs: {"git_commit": "commit", "ok": True},
    )
    monkeypatch.setattr(operator, "inspect_remote_run", lambda *args: current)
    monkeypatch.setattr(operator, "reconcile_remote_run", lambda *args: {"ok": True})
    monkeypatch.setattr(
        operator,
        "run_remote_pipeline",
        lambda *args, **kwargs: pytest.fail("expensive stage executed"),
    )

    def fake_copy(*args, **kwargs):
        destination.mkdir(parents=True, exist_ok=True)
        return destination

    monkeypatch.setattr(operator, "copy_review_artifacts", fake_copy)
    args = operator.build_parser().parse_args(
        [
            "resume",
            "--remote-host",
            "fixture-host",
            "--run-id",
            "refresh-run",
            "--refresh-artifacts",
        ]
    )
    exit_code, _ = operator.execute(args)
    assert exit_code == operator.EXIT_SUCCESS
    assert (destination / "operator-summary.json").is_file()


def test_summary_snapshot_is_inspected_after_remote_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = config(tmp_path)
    events: list[str] = []
    final_snapshot = completed_operator_snapshot("new-run")
    final_snapshot["current_metrics"]["ai_errors"] = 0
    final_snapshot["approval_summary"]["ai_errors"] = 816
    final_snapshot["artifacts"]["stage3/canonical.csv"]["exists"] = False
    monkeypatch.setattr(operator, "load_config", lambda **kwargs: cfg)
    monkeypatch.setattr(
        operator,
        "local_preflight",
        lambda *args, **kwargs: operator.LocalPreflight(
            "repo", "main", "commit", False, False, {}, str(tmp_path)
        ),
    )
    monkeypatch.setattr(operator, "RemoteExecutor", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        operator,
        "remote_preflight",
        lambda *args, **kwargs: {"git_commit": "commit", "ok": True},
    )
    monkeypatch.setattr(operator, "create_remote_run", lambda *args: "new-run")
    monkeypatch.setattr(
        operator,
        "run_remote_pipeline",
        lambda *args, **kwargs: events.append("pipeline_completed"),
    )

    def inspect(*args):
        assert events == ["pipeline_completed"]
        return final_snapshot

    monkeypatch.setattr(operator, "inspect_remote_run", inspect)
    args = operator.build_parser().parse_args(
        ["run", "--remote-host", "fixture-host"]
    )
    exit_code, result = operator.execute(args)
    assert exit_code == operator.EXIT_SUCCESS
    assert result["ai_errors"] == 0


def test_secret_redaction_and_windows_path_quoting() -> None:
    redacted = redact_secrets(
        "GEOAPIFY_API_KEY=secret DATABASE_URL=postgres://user:password@host/db "
        "Authorization: Bearer auth-secret PRIVATE_TOKEN=token-secret "
        "postgresql://dbuser:dbpass@dbhost/database"
    )
    assert "secret" not in redacted
    assert "postgres://" not in redacted
    assert "postgresql://" not in redacted
    assert "dbpass" not in redacted
    script = operator._remote_script(
        OperatorConfig(
            RemoteConfig(host="host", project_path=r"C:\A Path\repo"),
            LocalConfig(Path("reviews")),
            PipelineConfig(),
        ),
        ["-m", "pipeline.operator_remote", "inspect", "--run-id", "safe-run"],
    )
    assert "Set-Location -LiteralPath 'C:\\A Path\\repo'" in script
    assert "& 'C:\\A Path\\repo\\.venv-scraperai\\Scripts\\python.exe'" in script
    assert "$ErrorActionPreference = 'Stop'" in script
    assert "shell" not in script.casefold()
