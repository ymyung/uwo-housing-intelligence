from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from pipeline import canonical_rebuild
from pipeline.operator_summary import derive_current_metrics
from pipeline.run_approval import sha256_file
from pipeline.run_context import RunContext


GEOCODE = {
    "geocode_query": "123 Richmond Street, London, ON, Canada",
    "latitude": "43.01",
    "longitude": "-81.27",
    "geocode_status": "ok",
    "geocode_confidence": "0.95",
    "geocode_match_type": "full_match",
    "geocode_result_type": "building",
    "geocode_formatted": "123 Richmond Street, London, ON, Canada",
    "geocode_city": "London",
    "geocode_postcode": "N6A 1A1",
    "geocode_country_code": "ca",
    "geocode_error": "",
    "distance_to_western_km": "1.5",
}


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as source:
        return list(csv.DictReader(source))


def make_run(tmp_path: Path) -> RunContext:
    context = RunContext.create_at(
        tmp_path / "fixture-run",
        run_id="fixture-run",
        configuration={
            "stage3_qc": {
                "confidence_threshold": 0.8,
                "allow_low_confidence": False,
            }
        },
    )
    reviewed = [
        {
            "listing_id": "one",
            "listing_url": "https://example.test/listing/one",
            "price_monthly": "900",
            "ai_error": "",
            "needs_manual_review": "True",
            "manual_reviewed": "False",
            "manual_review_note": "Keep this correction",
            "bathrooms": "2",
        },
        {
            "listing_id": "two",
            "listing_url": "https://example.test/listing/two",
            "price_monthly": "1200",
            "ai_error": "",
            "needs_manual_review": "False",
            "manual_reviewed": "False",
            "manual_review_note": "",
            "bathrooms": "1",
        },
    ]
    geocoded = [
        {
            **reviewed[1],
            "ai_error": "stale failure",
            **GEOCODE,
        },
        {
            **reviewed[0],
            "bathrooms": "99",
            "ai_error": "stale failure",
            **GEOCODE,
            "geocode_confidence": "0.4",
        },
    ]
    stale = [{**row, **GEOCODE, "ai_error": "stale failure"} for row in reviewed]
    write_csv(context.paths.stage2_reviewed, reviewed)
    write_csv(context.paths.stage2_review_queue, [reviewed[0]])
    write_csv(context.paths.stage3_geocoded, geocoded)
    write_csv(context.paths.stage3_canonical, stale)
    write_csv(context.paths.stage3_geocode_review, [stale[0]])
    timestamp = "2026-08-01T00:00:00Z"
    stages = {}
    for name in (
        "stage0",
        "stage1",
        "stage2",
        "manual_fixes",
        "stage3",
        "stage3_qc",
    ):
        stages[name] = {
            "status": "completed",
            "started_at_utc": timestamp,
            "completed_at_utc": timestamp,
            "input_rows": 2,
            "output_rows": 2,
            "metrics": {},
        }
    stages["stage2"]["metrics"] = {"ai_error_count": 0, "review_count": 1}
    stages["manual_fixes"]["metrics"] = {"review_count": 1}
    stages["stage3"]["metrics"] = {"failed_geocode_count": 0}
    stages["stage3_qc"]["metrics"] = {"review_required_count": 1}
    context.manifest.update(
        {
            "status": "completed_with_warnings",
            "stages": stages,
            "errors": [],
            "error_history": [
                {
                    "error_id": "historical",
                    "stage": "stage2",
                    "resolved_at_utc": timestamp,
                }
            ],
        }
    )
    context.save()
    return context


def test_rebuild_uses_reviewed_authority_and_stable_id_geocodes(tmp_path: Path) -> None:
    context = make_run(tmp_path)

    result = canonical_rebuild.rebuild_canonical(context.paths.root)

    assert result["row_count"] == 2
    rows = read_csv(context.paths.stage3_canonical)
    assert [row["listing_id"] for row in rows] == ["one", "two"]
    assert [row["ai_error"] for row in rows] == ["", ""]
    assert rows[0]["manual_review_note"] == "Keep this correction"
    assert rows[0]["bathrooms"] == "2"
    assert rows[0]["price_monthly"] == "900"
    assert rows[0]["geocode_confidence"] == "0.4"
    assert rows[1]["geocode_confidence"] == "0.95"
    assert rows[0]["map_ready"] == "False"
    assert rows[1]["map_ready"] == "True"
    assert len(read_csv(context.paths.stage3_geocode_review)) == 1

    manifest = RunContext.resume(context.paths.root).manifest
    stage = manifest["stages"]["stage3_qc"]
    assert stage["started_at_utc"] == "2026-08-01T00:00:00Z"
    assert stage["completed_at_utc"] == "2026-08-01T00:00:00Z"
    assert manifest["error_history"] == context.manifest["error_history"]
    assert manifest["canonical_for_import"] is False
    assert (
        stage["output_metadata"]["stage3/canonical.csv"]["sha256"]
        == sha256_file(context.paths.stage3_canonical)
    )
    history = manifest["canonical_rebuild_history"]
    assert history[-1]["historical_metric_discrepancies"]
    assert history[-1]["inputs"]["stage2/reviewed.csv"]["row_count"] == 2
    assert derive_current_metrics(context.paths.root, manifest)["metric_discrepancies"] == []

    index = json.loads((context.paths.root / "review-index.json").read_text())
    assert index["categories"]["ai_review"]["count"] == 1
    assert index["categories"]["unresolved_manual_review"]["count"] == 1
    assert index["categories"]["geocoding_review"]["count"] == 1
    assert index["categories"]["missing_monthly_price"]["count"] == 0


def test_rebuild_second_call_is_content_idempotent(tmp_path: Path) -> None:
    context = make_run(tmp_path)
    first = canonical_rebuild.rebuild_canonical(context.paths.root)
    first_bytes = context.paths.stage3_canonical.read_bytes()
    first_manifest = RunContext.resume(context.paths.root).manifest
    timestamps = {
        key: first_manifest["stages"]["stage3_qc"][key]
        for key in ("started_at_utc", "completed_at_utc")
    }

    second = canonical_rebuild.rebuild_canonical(context.paths.root)
    second_manifest = RunContext.resume(context.paths.root).manifest

    assert first["idempotent"] is False
    assert second["idempotent"] is True
    assert context.paths.stage3_canonical.read_bytes() == first_bytes
    assert len(second_manifest["canonical_rebuild_history"]) == 1
    assert {
        key: second_manifest["stages"]["stage3_qc"][key] for key in timestamps
    } == timestamps


def test_rebuild_uses_atomic_replacement_for_both_csv_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = make_run(tmp_path)
    replacements: list[Path] = []
    real_replace = canonical_rebuild.os.replace

    def recording_replace(source: Path, target: Path) -> None:
        replacements.append(Path(target))
        real_replace(source, target)

    monkeypatch.setattr(canonical_rebuild.os, "replace", recording_replace)
    canonical_rebuild.rebuild_canonical(context.paths.root)
    assert context.paths.stage3_canonical.resolve() in replacements
    assert context.paths.stage3_geocode_review.resolve() in replacements
    assert not list(context.paths.stage3_dir.glob("*.tmp"))


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("duplicate_reviewed", "duplicate listing_id"),
        ("missing_id", "missing listing_id"),
        ("row_count", "row-count mismatch"),
        ("unmatched", "Unmatched listing IDs"),
        ("source_conflict", "Conflicting listing_url"),
    ],
)
def test_rebuild_rejects_unsafe_joins(
    tmp_path: Path, mutation: str, message: str
) -> None:
    context = make_run(tmp_path)
    reviewed = read_csv(context.paths.stage2_reviewed)
    geocoded = read_csv(context.paths.stage3_geocoded)
    if mutation == "duplicate_reviewed":
        reviewed[1]["listing_id"] = reviewed[0]["listing_id"]
    elif mutation == "missing_id":
        reviewed[0]["listing_id"] = ""
    elif mutation == "row_count":
        geocoded.pop()
    elif mutation == "unmatched":
        geocoded[0]["listing_id"] = "other"
    else:
        geocoded[0]["listing_url"] = "https://example.test/conflict"
    write_csv(context.paths.stage2_reviewed, reviewed)
    write_csv(context.paths.stage3_geocoded, geocoded)
    old_canonical = context.paths.stage3_canonical.read_bytes()

    with pytest.raises(canonical_rebuild.CanonicalRebuildError, match=message):
        canonical_rebuild.rebuild_canonical(context.paths.root)
    assert context.paths.stage3_canonical.read_bytes() == old_canonical
