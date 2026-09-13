import json
import sys
from pathlib import Path

import pandas as pd
import pytest

from pipeline import ai_enricher, geocoder
from pipeline.run_context import (
    COMPLETED,
    COMPLETED_WITH_WARNINGS,
    FAILED,
    SKIPPED,
    RunContext,
    determine_run_completion,
)
from scripts import apply_geocode_qc, apply_manual_review_fixes


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(path, index=False)


def test_stage2_run_paths_and_manifest_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = RunContext.create(tmp_path, run_id="stage2-run")
    write_csv(context.paths.stage1_website_ready, [{"listing_id": "1"}, {"listing_id": "2"}])

    def fake_enrich(**kwargs):
        assert kwargs["input_csv"] == context.paths.stage1_website_ready
        assert kwargs["output_csv"] == context.paths.stage2_enriched
        assert kwargs["review_queue_csv"] == context.paths.stage2_review_queue
        result = pd.DataFrame(
            {
                "listing_id": ["1", "2"],
                "needs_manual_review": [True, False],
                "ai_error": ["fixture error", None],
            }
        )
        result.to_csv(kwargs["output_csv"], index=False)
        result.iloc[:1].to_csv(kwargs["review_queue_csv"], index=False)
        return result

    monkeypatch.setattr(ai_enricher, "enrich_csv", fake_enrich)
    monkeypatch.setattr(
        sys, "argv", ["ai_enricher.py", "--run-dir", str(context.paths.root), "--resume"]
    )
    ai_enricher.main()

    stage = RunContext.resume(context.paths.root).manifest["stages"]["stage2"]
    assert stage["status"] == COMPLETED_WITH_WARNINGS
    assert stage["input_rows"] == stage["output_rows"] == 2
    assert stage["metrics"] == {
        "review_count": 1,
        "ai_call_count": 0,
        "ai_error_count": 1,
        "output_rows": 2,
    }


def test_systemic_ai_errors_fail_stage_and_preserve_resume_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = RunContext.create(tmp_path, run_id="systemic-ai-run")
    write_csv(context.paths.stage1_website_ready, [{"listing_id": str(i)} for i in range(10)])

    def fake_enrich(**kwargs):
        result = pd.DataFrame(
            {
                "listing_id": [str(i) for i in range(10)],
                "needs_manual_review": [True] * 10,
                "ai_skipped": [False] * 10,
                "ai_error": ["Connection refused at a private endpoint"] * 10,
            }
        )
        result.to_csv(kwargs["output_csv"], index=False)
        return result

    monkeypatch.setattr(ai_enricher, "enrich_csv", fake_enrich)
    monkeypatch.setattr(
        sys, "argv", ["ai_enricher.py", "--run-dir", str(context.paths.root), "--resume"]
    )
    with pytest.raises(ai_enricher.SystemicAIServiceError):
        ai_enricher.main()

    manifest = RunContext.resume(context.paths.root).manifest
    stage = manifest["stages"]["stage2"]
    assert stage["status"] == FAILED
    assert stage["metrics"]["ai_error_count"] == 10
    assert stage["metrics"]["output_rows"] == 10
    assert "stage3" not in manifest["stages"]
    assert "ollama_connection_failure" in manifest["errors"][0]["message"]
    assert "private endpoint" not in manifest["errors"][0]["message"]


def test_isolated_ai_error_completes_with_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = RunContext.create(tmp_path, run_id="isolated-ai-run")
    write_csv(context.paths.stage1_website_ready, [{"listing_id": str(i)} for i in range(10)])

    def fake_enrich(**kwargs):
        result = pd.DataFrame(
            {
                "listing_id": [str(i) for i in range(10)],
                "needs_manual_review": [True] + [False] * 9,
                "ai_skipped": [False] * 10,
                "ai_error": ["one malformed response"] + [None] * 9,
            }
        )
        result.to_csv(kwargs["output_csv"], index=False)
        return result

    monkeypatch.setattr(ai_enricher, "enrich_csv", fake_enrich)
    monkeypatch.setattr(
        sys, "argv", ["ai_enricher.py", "--run-dir", str(context.paths.root), "--resume"]
    )
    ai_enricher.main()

    stage = RunContext.resume(context.paths.root).manifest["stages"]["stage2"]
    assert stage["status"] == COMPLETED_WITH_WARNINGS
    assert stage["metrics"]["ai_error_count"] == 1


def test_manual_fix_run_paths_and_skipped_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = RunContext.create(tmp_path, run_id="manual-run")
    write_csv(context.paths.stage2_enriched, [{"listing_id": "fixture"}])
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "apply_manual_review_fixes.py",
            "--run-dir",
            str(context.paths.root),
            "--resume",
            "--skip",
        ],
    )
    apply_manual_review_fixes.main()

    assert context.paths.stage2_reviewed.read_text() == context.paths.stage2_enriched.read_text()
    stage = RunContext.resume(context.paths.root).manifest["stages"]["manual_fixes"]
    assert stage["status"] == SKIPPED
    assert stage["input_paths"] == [str(context.paths.stage2_enriched)]
    assert stage["output_paths"] == [str(context.paths.stage2_reviewed)]


def test_stage3_input_prefers_reviewed_then_falls_back(tmp_path: Path) -> None:
    context = RunContext.create(tmp_path, run_id="selection-run")
    write_csv(context.paths.stage2_enriched, [{"address": "1 Main St"}])
    assert geocoder.select_run_input(context) == context.paths.stage2_enriched
    write_csv(context.paths.stage2_reviewed, [{"address": "2 Main St"}])
    assert geocoder.select_run_input(context) == context.paths.stage2_reviewed


def cached_result(query: str, *, confidence: float = 0.95, status: str = "ok") -> dict:
    return {
        "geocode_query": query,
        "latitude": 43.0 if status == "ok" else None,
        "longitude": -81.2 if status == "ok" else None,
        "geocode_status": status,
        "geocode_confidence": confidence,
        "geocode_match_type": "full_match",
        "geocode_result_type": "building",
        "geocode_formatted": query,
        "geocode_city": "London",
        "geocode_postcode": "N6A 1A1",
        "geocode_country_code": "ca",
        "geocode_error": None,
    }


def test_cache_hits_cache_only_misses_and_no_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "input.csv"
    output = tmp_path / "output.csv"
    cache = tmp_path / "cache.csv"
    write_csv(source, [{"address": "1 Main St"}, {"address": "2 Main St"}])
    query = geocoder.normalize_address("1 Main St")
    pd.DataFrame([cached_result(query)]).to_csv(cache, index=False)

    def forbidden_network(*args, **kwargs):
        raise AssertionError("network call attempted")

    monkeypatch.setattr(geocoder, "geocode_address", forbidden_network)
    stats: dict[str, int] = {}
    result = geocoder.apply_geocoding(
        source, output, cache, None, cache_only=True, stats=stats, sleep_seconds=0
    )
    assert stats["cache_hit_count"] == 1
    assert stats["cache_miss_count"] == 1
    assert stats["new_api_call_count"] == 0
    assert result["geocode_status"].tolist() == ["ok", "cache_miss"]


def test_geocoding_completion_output_is_windows_console_safe(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "input.csv"
    output = tmp_path / "output.csv"
    cache = tmp_path / "cache.csv"
    write_csv(source, [{"address": "1 Main St"}])
    query = geocoder.normalize_address("1 Main St")
    pd.DataFrame([cached_result(query)]).to_csv(cache, index=False)

    geocoder.apply_geocoding(source, output, cache, None, cache_only=True)

    assert "Saved geocoded listings ->" in capsys.readouterr().out


def test_cache_refresh_selection() -> None:
    assert geocoder.should_refresh_cached_result(
        cached_result("x", status="error"), refresh_errors=True
    )
    assert not geocoder.should_refresh_cached_result(
        cached_result("x"), refresh_errors=True
    )
    assert geocoder.should_refresh_cached_result(
        cached_result("x", confidence=0.4),
        refresh_low_confidence=True,
        confidence_threshold=0.8,
    )
    assert not geocoder.should_refresh_cached_result(
        cached_result("x", confidence=0.9),
        refresh_low_confidence=True,
        confidence_threshold=0.8,
    )


def test_geocoding_run_manifest_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = RunContext.create(tmp_path, run_id="geocode-run")
    write_csv(context.paths.stage2_enriched, [{"address": "uncached"}, {"address": None}])
    cache = tmp_path / "external-cache.csv"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "geocoder.py",
            "--run-dir",
            str(context.paths.root),
            "--resume",
            "--cache-only",
            "--cache-csv",
            str(cache),
        ],
    )
    geocoder.main()
    stage = RunContext.resume(context.paths.root).manifest["stages"]["stage3"]
    assert stage["input_rows"] == stage["output_rows"] == 2
    assert stage["metrics"]["cache_miss_count"] == 1
    assert stage["metrics"]["missing_address_count"] == 1
    assert stage["metrics"]["new_api_call_count"] == 0


def test_geocoder_loads_repository_env_independent_of_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root = tmp_path / "repository"
    project_root.mkdir()
    (project_root / ".env").write_text(
        "GEOAPIFY_API_KEY=temporary-test-key\n", encoding="utf-8"
    )
    working_directory = tmp_path / "elsewhere"
    working_directory.mkdir()
    source = tmp_path / "input.csv"
    output = tmp_path / "output.csv"
    cache = tmp_path / "cache.csv"
    write_csv(source, [{"address": "1 Main Street"}])
    captured: dict[str, str | None] = {}

    def fake_apply_geocoding(**kwargs):
        captured["api_key"] = kwargs["api_key"]
        return pd.DataFrame([{"geocode_status": "ok"}])

    monkeypatch.setattr(geocoder, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(geocoder, "apply_geocoding", fake_apply_geocoding)
    monkeypatch.delenv("GEOAPIFY_API_KEY", raising=False)
    monkeypatch.chdir(working_directory)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "geocoder.py",
            str(source),
            "--output-csv",
            str(output),
            "--cache-csv",
            str(cache),
        ],
    )

    geocoder.main()

    assert captured["api_key"] == "temporary-test-key"


def qc_row(**updates) -> dict:
    row = {
        "listing_id": "1",
        "latitude": 43.0,
        "longitude": -81.2,
        "geocode_status": "ok",
        "geocode_confidence": 0.95,
        "geocode_match_type": "full_match",
        "geocode_result_type": "building",
        "geocode_city": "London",
        "geocode_country_code": "ca",
        "distance_to_western_km": 2,
    }
    row.update(updates)
    return row


@pytest.mark.parametrize(
    "updates",
    [
        {"latitude": None},
        {"geocode_status": "error"},
        {"geocode_city": "Toronto"},
        {"geocode_country_code": "us"},
        {"geocode_confidence": 0.4},
    ],
)
def test_qc_default_is_conservative(updates: dict) -> None:
    ready, _ = apply_geocode_qc.evaluate_geocode(
        pd.Series(qc_row(**updates)), confidence_threshold=0.8, allow_low_confidence=False
    )
    assert not ready


def test_qc_can_explicitly_allow_low_confidence() -> None:
    ready, issues = apply_geocode_qc.evaluate_geocode(
        pd.Series(qc_row(geocode_confidence=0.4)),
        confidence_threshold=0.8,
        allow_low_confidence=True,
    )
    assert ready
    assert "low_confidence" in issues


def test_qc_run_outputs_use_standard_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = RunContext.create(tmp_path, run_id="qc-run")
    write_csv(context.paths.stage3_geocoded, [qc_row(), qc_row(listing_id="2", latitude=None)])
    monkeypatch.setattr(
        sys,
        "argv",
        ["apply_geocode_qc.py", "--run-dir", str(context.paths.root), "--resume"],
    )
    apply_geocode_qc.main()
    assert context.paths.stage3_canonical.exists()
    assert context.paths.stage3_geocode_review.exists()
    stage = RunContext.resume(context.paths.root).manifest["stages"]["stage3_qc"]
    assert stage["metrics"]["map_ready_count"] == 1
    assert stage["metrics"]["review_required_count"] == 1


def complete_required_stages(context: RunContext, *, warning: bool = False) -> None:
    for stage_name in ("stage0", "stage1", "stage2", "manual_fixes", "stage3"):
        context.start_stage(stage_name)
        metrics = {"review_count": 1} if warning and stage_name == "stage2" else None
        context.finish_stage(stage_name, input_rows=2, output_rows=2, metrics=metrics)
    context.start_stage("stage3_qc")
    context.finish_stage("stage3_qc", input_rows=2, output_rows=2)
    context.paths.stage3_canonical.write_text("listing_id\n1\n2\n", encoding="utf-8")


def test_complete_and_completed_with_warnings_status(tmp_path: Path) -> None:
    clean = RunContext.create(tmp_path, run_id="clean")
    complete_required_stages(clean)
    assert determine_run_completion(clean)[0] == COMPLETED

    warning = RunContext.create(tmp_path, run_id="warning")
    complete_required_stages(warning, warning=True)
    assert determine_run_completion(warning)[0] == COMPLETED_WITH_WARNINGS


def test_failed_stage_and_row_mismatch_fail_completion(tmp_path: Path) -> None:
    failed = RunContext.create(tmp_path, run_id="failed")
    complete_required_stages(failed)
    failed.manifest["stages"]["stage3"]["status"] = FAILED
    assert determine_run_completion(failed)[0] == FAILED

    mismatch = RunContext.create(tmp_path, run_id="mismatch")
    complete_required_stages(mismatch)
    mismatch.manifest["stages"]["stage3"]["output_rows"] = 1
    assert determine_run_completion(mismatch)[0] == FAILED


def test_powershell_is_repo_relative_and_legacy_cli_parsers_work() -> None:
    script = Path("scripts/run_full_pipeline.ps1").read_text(encoding="utf-8")
    assert "C:\\Users\\" not in script
    assert "$PSScriptRoot" in script
    stage2 = ai_enricher.build_parser().parse_args(
        ["input.csv", "--output-csv", "output.csv"]
    )
    stage3 = geocoder.build_parser().parse_args(
        ["input.csv", "--output-csv", "output.csv"]
    )
    manual = apply_manual_review_fixes.build_parser().parse_args(
        ["--input-csv", "input.csv", "--output-csv", "output.csv"]
    )
    qc = apply_geocode_qc.build_parser().parse_args(
        ["--input-csv", "input.csv", "--output-csv", "output.csv"]
    )
    assert stage2.input_csv == Path("input.csv")
    assert stage3.input_csv == Path("input.csv")
    assert manual.output_csv == Path("output.csv")
    assert qc.output_csv == Path("output.csv")
