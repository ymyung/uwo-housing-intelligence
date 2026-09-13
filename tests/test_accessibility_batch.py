from __future__ import annotations

import csv
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.accessibility_inputs import AccessibilityInputError
from scripts import run_accessibility_batch as batch


def _properties(path: Path, count: int) -> Path:
    path.write_text(
        "property_id,latitude,longitude,normalized_address,review_status\n"
        + "".join(
            f"{index},43.0,-81.25,{index} Test Street,approved\n"
            for index in range(1, count + 1)
        ),
        encoding="utf-8",
    )
    return path


def _worker_artifacts(
    root: Path,
    run_id: str,
    property_ids: list[int],
    *,
    warning: bool = False,
) -> dict[str, object]:
    run_root = root / run_id
    run_root.mkdir(parents=True)
    _properties(run_root / "selected-properties.csv", len(property_ids))
    rows = (run_root / "selected-properties.csv").read_text(encoding="utf-8")
    for sequential, actual in enumerate(property_ids, start=1):
        rows = rows.replace(f"\n{sequential},", f"\n{actual},", 1)
    (run_root / "selected-properties.csv").write_text(rows, encoding="utf-8")
    quality_fields = [
        "property_id",
        "address",
        "hotspot",
        "mode",
        "time_period",
        "quality_status",
        "decision",
        "reason_codes",
    ]
    with (run_root / "quality-report.csv").open(
        "w", encoding="utf-8", newline=""
    ) as target:
        writer = csv.DictWriter(target, fieldnames=quality_fields, lineterminator="\n")
        writer.writeheader()
        for property_id in property_ids:
            writer.writerow(
                {
                    "property_id": property_id,
                    "address": f"{property_id} Test Street",
                    "hotspot": "Western",
                    "mode": "walking",
                    "time_period": "",
                    "quality_status": "complete",
                    "decision": "accepted_with_warning" if warning else "accepted",
                    "reason_codes": "excessive_detour" if warning else "",
                }
            )
    (run_root / "review-required.csv").write_text(
        "unit_key,property_id,address,mode,time_period,decision,reason_codes,error_type,error_message\n",
        encoding="utf-8",
    )
    result: dict[str, object] = {
        "run_id": run_id,
        "status": "completed_with_warnings" if warning else "completed",
        "completed_at": "2026-08-08T12:00:00Z",
        "metrics": {
            "cache_hits": 0,
            "sample_cache_hits": 0,
            "provider_calls": len(property_ids),
            "provider_retries": 0,
            "profiles_created": len(property_ids),
            "profiles_refreshed": 0,
            "profiles_skipped": 0,
            "profiles_failed": 0,
            "database_writes": len(property_ids),
        },
        "success_count": len(property_ids),
        "failure_count": 0,
        "quality_warnings": len(property_ids) if warning else 0,
    }
    (run_root / "manifest.json").write_text(
        json.dumps(
            {
                **result,
                "started_at": "2026-08-08T11:00:00Z",
                "requested_modes": ["walking", "cycling", "transit"],
            }
        ),
        encoding="utf-8",
    )
    return result


def test_property_loading_preserves_order_and_rejects_duplicates(tmp_path: Path) -> None:
    path = _properties(tmp_path / "properties.csv", 3)
    assert batch.load_property_ids(path) == [1, 2, 3]
    path.write_text(
        "property_id,latitude,longitude,normalized_address,review_status\n"
        "2,43,-81,Two,approved\n2,43,-81,Two,approved\n",
        encoding="utf-8",
    )
    with pytest.raises(AccessibilityInputError, match="duplicate property_id 2"):
        batch.load_property_ids(path)


def test_batch_plan_is_deterministic_and_never_exceeds_worker_limit() -> None:
    plan = batch.build_batch_plan(list(range(1, 51)), 20)
    assert [value["property_count"] for value in plan] == [20, 20, 10]
    assert [value["batch_id"] for value in plan] == [
        "batch-001",
        "batch-002",
        "batch-003",
    ]
    assert [item for value in plan for item in value["property_ids"]] == list(
        range(1, 51)
    )
    with pytest.raises(AccessibilityInputError, match="between 1 and 20"):
        batch.build_batch_plan([1], 21)


def test_dry_run_uses_bounded_worker_calls_and_aggregates_estimates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    properties = _properties(tmp_path / "properties.csv", 45)
    calls = []

    def fake_run(args, *, resume):
        assert resume is False
        assert args.allow_larger_run is False
        ids = [int(value) for value in args.property_ids.split(",")]
        calls.append(ids)
        return {
            "estimate": {
                "cache_hits": len(ids),
                "sample_cache_hits": 0,
                "provider_calls": 0,
                "provider_retries": 0,
                "profiles_created": 0,
                "profiles_refreshed": 0,
                "profiles_skipped": len(ids),
                "profiles_failed": 0,
                "database_writes": 0,
                "expected_provider_calls": len(ids) * 2,
                "expected_database_writes": len(ids),
            }
        }

    monkeypatch.setattr(batch.worker_cli, "_run", fake_run)
    result = batch.dry_run_batches(
        properties_path=properties,
        config_path=tmp_path / "config.toml",
        hotspot_ids=["western-main-campus"],
        modes=["walking", "cycling", "transit"],
        batch_size=20,
    )
    assert [len(value) for value in calls] == [20, 20, 5]
    assert result["property_count"] == 45
    assert result["batch_count"] == 3
    assert result["profile_count"] == 360
    assert result["estimate"]["expected_provider_calls"] == 90
    assert result["provider_calls_made"] == 0
    assert result["database_writes_made"] == 0

    calls.clear()
    walking_only = batch.dry_run_batches(
        properties_path=properties,
        config_path=tmp_path / "config.toml",
        hotspot_ids=["western-main-campus"],
        modes=["walking"],
        batch_size=20,
    )
    assert walking_only["profile_count"] == 45


def test_execution_aggregates_worker_manifests_and_warning_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    properties = _properties(tmp_path / "properties.csv", 3)
    worker_root = tmp_path / "worker-runs"
    monkeypatch.setattr(
        batch,
        "load_worker_config",
        lambda _: SimpleNamespace(run_root=worker_root),
    )
    counter = 0

    def fake_run(args, *, resume):
        nonlocal counter
        assert resume is False
        counter += 1
        ids = [int(value) for value in args.property_ids.split(",")]
        return _worker_artifacts(
            worker_root, f"20260808T12000{counter}000000Z_fixture", ids, warning=counter == 2
        )

    monkeypatch.setattr(batch.worker_cli, "_run", fake_run)
    store = batch.AccessibilityBatchStore.create(
        tmp_path / "batch-runs",
        properties_path=properties,
        property_ids=[1, 2, 3],
        config_path=tmp_path / "config.toml",
        hotspot_ids=["western-main-campus"],
        modes=["walking", "cycling", "transit"],
        batch_size=2,
        continue_on_failure=False,
    )
    result = batch.execute_batches(store, resume=False)
    assert result["status"] == "completed_with_warnings"
    assert result["aggregate"]["profiles_requested"] == 3
    assert result["aggregate"]["metrics"]["provider_calls"] == 3
    assert result["aggregate"]["metrics"]["database_writes"] == 3
    warning_rows = list(
        csv.DictReader(store.paths.warnings.open(encoding="utf-8", newline=""))
    )
    assert [row["property_id"] for row in warning_rows] == ["3"]
    assert {row["batch_id"] for row in warning_rows} == {"batch-002"}


def test_resume_skips_completed_batches_and_retries_incomplete_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    properties = _properties(tmp_path / "properties.csv", 3)
    worker_root = tmp_path / "worker-runs"
    monkeypatch.setattr(
        batch,
        "load_worker_config",
        lambda _: SimpleNamespace(run_root=worker_root),
    )
    store = batch.AccessibilityBatchStore.create(
        tmp_path / "batch-runs",
        properties_path=properties,
        property_ids=[1, 2, 3],
        config_path=tmp_path / "config.toml",
        hotspot_ids=["western-main-campus"],
        modes=["walking", "cycling", "transit"],
        batch_size=2,
        continue_on_failure=False,
    )
    manifest = store.read()
    first = _worker_artifacts(
        worker_root, "20260808T120001000000Z_fixture", [1, 2]
    )
    batch._apply_worker_result(manifest["batches"][0], first, worker_root)
    manifest["batches"][1]["status"] = "failed"
    store.write(manifest)
    calls = []

    def fake_run(args, *, resume):
        assert resume is False
        ids = [int(value) for value in args.property_ids.split(",")]
        calls.append(ids)
        return _worker_artifacts(
            worker_root, "20260808T120002000000Z_fixture", ids
        )

    monkeypatch.setattr(batch.worker_cli, "_run", fake_run)
    result = batch.execute_batches(store, resume=True)
    assert calls == [[3]]
    assert result["status"] == "completed"
    assert [value["status"] for value in result["batches"]] == [
        "completed",
        "completed",
    ]


def test_resume_rejects_changed_source_property_csv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    properties = _properties(tmp_path / "properties.csv", 2)
    store = batch.AccessibilityBatchStore.create(
        tmp_path / "batch-runs",
        properties_path=properties,
        property_ids=[1, 2],
        config_path=tmp_path / "config.toml",
        hotspot_ids=["western-main-campus"],
        modes=["walking"],
        batch_size=2,
        continue_on_failure=False,
    )
    properties.write_text(properties.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    monkeypatch.setattr(
        batch,
        "load_worker_config",
        lambda _: SimpleNamespace(run_root=tmp_path / "worker-runs"),
    )
    with pytest.raises(AccessibilityInputError, match="changed or is missing"):
        batch.execute_batches(store, resume=True)


def test_discovery_matches_worker_canonical_property_order(tmp_path: Path) -> None:
    worker_root = tmp_path / "worker-runs"
    run_id = "20260808T120001000000Z_fixture"
    result = _worker_artifacts(worker_root, run_id, [1, 3])
    parent = {
        "started_at": "2026-08-08T11:00:00Z",
        "modes": ["walking", "cycling", "transit"],
    }
    discovered = batch._discover_worker_run(
        worker_root,
        parent,
        {"property_ids": [3, 1]},
    )
    assert discovered is not None
    assert discovered["run_id"] == result["run_id"]
