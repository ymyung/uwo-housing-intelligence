import json
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest
import pipeline.run_context as run_context_module

from pipeline.run_context import (
    COMPLETED,
    COMPLETED_WITH_WARNINGS,
    FAILED,
    RUNNING,
    RunContext,
    RunPaths,
    atomic_write_json,
    generate_run_id,
    validate_discovery,
    validate_run_id,
)


def test_run_ids_are_unique_and_filesystem_safe() -> None:
    now = datetime(2026, 7, 18, 15, 30, 45, tzinfo=timezone.utc)
    run_ids = {generate_run_id(now=now) for _ in range(20)}

    assert len(run_ids) == 20
    assert all(re.fullmatch(r"[A-Za-z0-9._-]+", run_id) for run_id in run_ids)


@pytest.mark.parametrize("run_id", ["../escape", "nested/run", "nested\\run", ".."])
def test_unsafe_run_ids_are_rejected(run_id: str) -> None:
    with pytest.raises(ValueError):
        validate_run_id(run_id)


def test_run_directories_and_standard_paths_are_created(tmp_path: Path) -> None:
    context = RunContext.create(tmp_path / "runs", run_id="test-run")

    assert context.paths.manifest.exists()
    assert context.paths.stage0_dir.is_dir()
    assert context.paths.stage1_dir.is_dir()
    assert context.paths.stage2_dir.is_dir()
    assert context.paths.stage3_dir.is_dir()
    assert context.paths.logs_dir.is_dir()
    assert context.paths.stage0_listing_links == (
        tmp_path / "runs" / "test-run" / "stage0" / "listing_links.csv"
    ).resolve()
    assert context.paths.stage1_details.name == "details.csv"
    assert context.paths.stage1_website_ready.name == "website_ready.csv"
    assert context.paths.stage1_checkpoint.name == "checkpoint.csv"


def test_manifest_initialization_contains_required_fields(tmp_path: Path) -> None:
    context = RunContext.create(tmp_path, run_id="manifest-test")
    manifest = json.loads(context.paths.manifest.read_text(encoding="utf-8"))

    required = {
        "run_id",
        "created_at_utc",
        "updated_at_utc",
        "status",
        "git_commit",
        "git_dirty",
        "hostname",
        "platform",
        "python_version",
        "command",
        "configuration",
        "stages",
        "warnings",
        "errors",
    }
    assert required <= manifest.keys()
    assert manifest["status"] == RUNNING
    assert manifest["error_history"] == []


def test_stage_transitions_and_row_counts_are_persisted(tmp_path: Path) -> None:
    context = RunContext.create(tmp_path, run_id="transition-test")
    input_path = context.paths.stage0_listing_links
    output_path = context.paths.stage1_details

    context.start_stage("stage1", input_paths=[input_path], output_paths=[output_path])
    context.finish_stage("stage1", input_rows=5, output_rows=5)

    persisted = RunContext.resume(context.paths.root).manifest
    stage = persisted["stages"]["stage1"]
    assert stage["status"] == COMPLETED
    assert stage["input_rows"] == 5
    assert stage["output_rows"] == 5
    assert stage["completed_at_utc"] is not None
    assert persisted["status"] == RUNNING


def test_stage_warnings_are_distinct_from_errors(tmp_path: Path) -> None:
    context = RunContext.create(tmp_path, run_id="warning-test")
    context.start_stage("stage0")
    context.finish_stage("stage0", output_rows=0, warnings=["No listings"])

    persisted = RunContext.resume(context.paths.root).manifest
    assert persisted["stages"]["stage0"]["status"] == COMPLETED_WITH_WARNINGS
    assert persisted["stages"]["stage0"]["warning_count"] == 1
    assert persisted["errors"] == []


def test_failed_stage_is_recorded_before_error_propagation(tmp_path: Path) -> None:
    context = RunContext.create(tmp_path, run_id="failure-test")
    context.start_stage("stage1")
    context.fail_stage("stage1", RuntimeError("fixture failure"))

    persisted = RunContext.resume(context.paths.root).manifest
    assert persisted["status"] == FAILED
    assert persisted["stages"]["stage1"]["status"] == FAILED
    assert persisted["stages"]["stage1"]["error_count"] == 1
    assert persisted["errors"][0]["message"] == "fixture failure"
    assert persisted["error_history"][0]["message"] == "fixture failure"
    assert persisted["error_history"][0]["resolved_at_utc"] is None


def test_successful_resume_resolves_only_same_stage_active_errors(
    tmp_path: Path,
) -> None:
    context = RunContext.create(tmp_path, run_id="resolved-error-test")
    context.start_stage("stage2")
    context.fail_stage("stage2", RuntimeError("Ollama unavailable"))
    context.start_stage("stage3")
    context.fail_stage("stage3", RuntimeError("Geoapify unavailable"))

    context.start_stage("stage2")
    context.finish_stage("stage2", input_rows=3, output_rows=3)

    persisted = RunContext.resume(context.paths.root).manifest
    assert [error["stage"] for error in persisted["errors"]] == ["stage3"]
    stage2_history = [
        error
        for error in persisted["error_history"]
        if error["stage"] == "stage2"
    ]
    stage3_history = [
        error
        for error in persisted["error_history"]
        if error["stage"] == "stage3"
    ]
    assert stage2_history[0]["resolved_at_utc"] is not None
    assert stage3_history[0]["resolved_at_utc"] is None


def test_legacy_errors_reconcile_after_success_and_preserve_every_attempt(
    tmp_path: Path,
) -> None:
    context = RunContext.create(tmp_path, run_id="legacy-reconciliation")
    timestamps = [
        "2026-08-01T06:05:43.485752Z",
        "2026-08-01T23:23:44.799031Z",
        "2026-08-01T23:23:59.048881Z",
    ]
    context.manifest["errors"] = [
        {
            "stage": "stage3",
            "type": "RuntimeError",
            "message": "Missing GEOAPIFY_API_KEY environment variable.",
            "at_utc": timestamp,
        }
        for timestamp in timestamps
    ]
    context.manifest["error_history"] = None
    context.manifest["stages"]["stage3"] = {
        "status": COMPLETED_WITH_WARNINGS,
        "completed_at_utc": "2026-08-02T01:00:00Z",
        "attempt_id": "successful-stage3-attempt",
    }

    assert context.reconcile_successful_stage_errors() == 3
    assert context.manifest["errors"] == []
    history = context.manifest["error_history"]
    assert len(history) == 3
    assert [item["at_utc"] for item in history] == timestamps
    assert len({item["error_id"] for item in history}) == 3
    assert all(item["active"] is False for item in history)
    assert all(item["legacy_source"] is True for item in history)
    assert all(item["resolution_reason"] == "stage_completed_successfully" for item in history)
    assert all(item["resolved_at_utc"] == "2026-08-02T01:00:00Z" for item in history)
    assert all(item["resolving_attempt_id"] == "successful-stage3-attempt" for item in history)

    before = json.loads(json.dumps(history))
    assert context.reconcile_successful_stage_errors() == 0
    assert context.manifest["error_history"] == before


def test_legacy_error_stays_active_while_stage_failed_and_other_success_is_unrelated(
    tmp_path: Path,
) -> None:
    context = RunContext.create(tmp_path, run_id="legacy-active")
    stage3_error = {
        "stage": "stage3",
        "type": "RuntimeError",
        "message": "Geoapify missing",
        "at_utc": "2026-08-01T01:00:00Z",
    }
    stage2_error = {
        "stage": "stage2",
        "type": "RuntimeError",
        "message": "Ollama missing",
        "at_utc": "2026-08-01T01:01:00Z",
    }
    context.manifest["errors"] = [stage3_error, stage2_error]
    context.manifest["error_history"] = []
    context.manifest["stages"] = {
        "stage3": {"status": FAILED, "completed_at_utc": "2026-08-01T01:02:00Z"},
        "stage2": {"status": COMPLETED, "completed_at_utc": "2026-08-01T01:03:00Z"},
    }

    assert context.reconcile_successful_stage_errors() == 1
    assert context.manifest["errors"] == [stage3_error]
    assert context.manifest["error_history"][0]["stage"] == "stage2"


def test_resolved_stage_can_fail_again_without_losing_history(tmp_path: Path) -> None:
    context = RunContext.create(tmp_path, run_id="later-stage-failure")
    context.manifest["errors"] = [
        {
            "stage": "stage3",
            "type": "RuntimeError",
            "message": "first failure",
            "at_utc": "2026-08-01T01:00:00Z",
        }
    ]
    context.manifest["error_history"] = []
    context.manifest["stages"]["stage3"] = {
        "status": COMPLETED,
        "completed_at_utc": "2026-08-01T02:00:00Z",
    }
    context.reconcile_successful_stage_errors()

    context.start_stage("stage3")
    context.fail_stage("stage3", RuntimeError("later failure"))
    persisted = RunContext.resume(context.paths.root).manifest
    assert [item["message"] for item in persisted["errors"]] == ["later failure"]
    assert len(persisted["error_history"]) == 2
    assert persisted["error_history"][0]["resolved_at_utc"] is not None
    assert persisted["error_history"][1]["resolved_at_utc"] is None


def test_atomic_manifest_writes_always_leave_valid_json(tmp_path: Path) -> None:
    manifest_path = tmp_path / "nested" / "manifest.json"

    for version in range(10):
        atomic_write_json(manifest_path, {"version": version})
        assert json.loads(manifest_path.read_text(encoding="utf-8")) == {
            "version": version
        }
        assert not list(manifest_path.parent.glob("*.tmp"))


def test_atomic_manifest_write_retries_transient_windows_file_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path = tmp_path / "manifest.json"
    actual_replace = run_context_module.os.replace
    attempts = 0

    def flaky_replace(source: Path, destination: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError(5, "transient Windows file lock")
        actual_replace(source, destination)

    monkeypatch.setattr(run_context_module.os, "replace", flaky_replace)
    monkeypatch.setattr(run_context_module.time, "sleep", lambda _seconds: None)

    atomic_write_json(manifest_path, {"status": "complete"})

    assert attempts == 3
    assert json.loads(manifest_path.read_text(encoding="utf-8")) == {
        "status": "complete"
    }


def test_existing_outputs_require_explicit_permission(tmp_path: Path) -> None:
    context = RunContext.create(tmp_path, run_id="overwrite-test")
    output = context.paths.stage0_listing_links
    output.write_text("item_page_link\n", encoding="utf-8")

    with pytest.raises(FileExistsError):
        context.ensure_outputs_available([output])

    context.ensure_outputs_available([output], allow_existing=True)


def test_existing_run_requires_explicit_resume_or_overwrite(tmp_path: Path) -> None:
    context = RunContext.create(tmp_path, run_id="resume-test")

    with pytest.raises(FileExistsError):
        RunContext.open_for_cli(
            context.paths.root,
            resume=False,
            overwrite=False,
            command=["fixture"],
            configuration={},
        )

    resumed = RunContext.open_for_cli(
        context.paths.root,
        resume=True,
        overwrite=False,
        command=["fixture"],
        configuration={},
    )
    assert resumed.manifest["run_id"] == "resume-test"


@pytest.mark.parametrize("parts", [("windows", "nested"), ("posix", "nested")])
def test_paths_use_cross_platform_pathlib_joining(
    tmp_path: Path, parts: tuple[str, str]
) -> None:
    root = tmp_path.joinpath(*parts)
    paths = RunPaths(root.resolve())

    assert paths.stage0_listing_links == root.resolve() / "stage0" / "listing_links.csv"
    assert paths.stage1_checkpoint == root.resolve() / "stage1" / "checkpoint.csv"


def test_discovery_warning_policy() -> None:
    warnings = validate_discovery(
        0,
        minimum_count=10,
        previous_successful_count=100,
        substantial_drop_ratio=0.5,
        repeated_page_detected=True,
        maximum_page_limit_reached=True,
    )

    assert len(warnings) == 5
    assert any("zero" in warning.lower() for warning in warnings)
    assert any("below minimum" in warning.lower() for warning in warnings)
    assert any("substantially" in warning.lower() for warning in warnings)
    assert any("repeated" in warning.lower() for warning in warnings)
    assert any("maximum" in warning.lower() for warning in warnings)
