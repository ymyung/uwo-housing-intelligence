from __future__ import annotations

import csv
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from pipeline import review_workflow
from pipeline.run_approval import approve_run, evaluate_run_approval
from pipeline.run_context import RunContext


def base_row(listing_id: str = "1001", **updates) -> dict[str, object]:
    row: dict[str, object] = {
        "listing_id": listing_id,
        "listing_url": f"https://offcampus.uwo.ca/Listings/Details/{listing_id}",
        "title": f"Listing {listing_id}",
        "description": "Quiet room near campus.",
        "address": "123 Richmond Street",
        "price_text": "$900 per month",
        "price_numeric": "900",
        "price_period": "month",
        "price_monthly": "900",
        "is_sublet": "False",
        "furnished": "",
        "utilities_included": "",
        "bathroom_type": "",
        "lease_term_months": "12",
        "needs_manual_review": "False",
        "manual_reviewed": "",
        "manual_review_note": "",
        "review_flags": "[]",
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
        "map_ready": "True",
        "geocode_quality_issue": "",
        "scraped_ok": "True",
    }
    row.update(updates)
    return row


def write_csv(path: Path, rows: list[dict[str, object]], fields=None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(fields or [])
    for row in rows:
        for field in row:
            if field not in fieldnames:
                fieldnames.append(field)
    with path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as source:
        return list(csv.DictReader(source))


def make_run(tmp_path: Path, rows: list[dict[str, object]], run_id="review-run") -> Path:
    context = RunContext.create_at(
        tmp_path / run_id,
        run_id=run_id,
        configuration={
            "stage0": {"max_pages": 100},
            "stage1": {"limit": None},
            "stage3": {"confidence_threshold": 0.8},
            "stage3_qc": {
                "confidence_threshold": 0.8,
                "allow_low_confidence": False,
            },
        },
    )
    write_csv(context.paths.stage3_canonical, rows)
    write_csv(context.paths.stage3_geocoded, rows)
    reviewed = [
        {key: value for key, value in row.items() if key not in review_workflow.GEOCODE_FIELDS}
        for row in rows
    ]
    write_csv(context.paths.stage2_reviewed, reviewed)
    queue = [row for row in reviewed if str(row.get("needs_manual_review", "")).lower() == "true"]
    write_csv(
        context.paths.stage2_review_queue,
        queue,
        fields=list(reviewed[0]),
    )
    geocode_review = [row for row in rows if str(row.get("map_ready", "")).lower() != "true"]
    write_csv(context.paths.stage3_geocode_review, geocode_review, fields=list(rows[0]))
    write_csv(
        context.paths.stage0_listing_links,
        [{"item_page_link": row["listing_url"]} for row in rows],
    )
    timestamp = "2026-08-02T00:00:00Z"
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
            "input_rows": None if name == "stage0" else len(rows),
            "output_rows": len(rows),
            "error_count": 0,
            "warning_count": 0,
            "duration_seconds": 1.0,
            "metrics": {},
        }
    stages["stage0"]["metrics"] = {
        "discovered_listing_count": len(rows),
        "maximum_page_limit_reached": False,
    }
    stages["stage2"]["metrics"] = {
        "ai_error_count": 0,
        "review_count": len(queue),
    }
    stages["manual_fixes"]["metrics"] = {"review_count": len(queue)}
    stages["stage3"]["metrics"] = {"failed_geocode_count": 0}
    stages["stage3_qc"]["metrics"] = {
        "review_required_count": len(geocode_review)
    }
    context.manifest.update(
        {
            "status": "completed",
            "created_at_utc": timestamp,
            "updated_at_utc": timestamp,
            "stages": stages,
            "errors": [],
            "warnings": [],
            "canonical_for_import": False,
        }
    )
    context.save()
    return context.paths.root


def decisions(run_dir: Path) -> list[dict]:
    path = run_dir / "review" / "review-decisions.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def current_decisions(run_dir: Path) -> list[dict]:
    canonical = {row["listing_id"]: row for row in read_csv(run_dir / "stage3/canonical.csv")}
    return [
        item
        for item in decisions(run_dir)
        if item["listing_id"] in canonical
        and item["input_fingerprint"]
        == review_workflow.listing_fingerprint(canonical[item["listing_id"]])
    ]


def test_structured_rule_and_explicit_text_are_automatic(tmp_path: Path) -> None:
    rows = [
        base_row(
            "1001",
            furnished="False",
            furnished_rule="True",
            furnished_source="ai",
            review_flags='["furnished_false_but_description_mentions_furnished"]',
            needs_manual_review="True",
        ),
        base_row(
            "1002",
            is_sublet="False",
            description="Explicit sublet available from September.",
            review_flags='["is_sublet_true_but_ai_confidence_low"]',
            needs_manual_review="True",
        ),
    ]
    run_dir = make_run(tmp_path, rows)
    review_workflow.run_automated_review(
        run_dir, now=datetime(2026, 8, 2, tzinfo=timezone.utc)
    )
    items = current_decisions(run_dir)

    furnished = next(item for item in items if item["field"] == "furnished")
    sublet = next(item for item in items if item["listing_id"] == "1002")
    assert furnished["decision_status"] == "auto_resolved"
    assert furnished["evidence_source"] == "deterministic_parser"
    assert sublet["decision_status"] == "auto_resolved"
    assert sublet["selected_value"] is True
    assert sublet["supporting_text"] == ["sublet"]


def test_summer_availability_never_implies_sublet(tmp_path: Path) -> None:
    run_dir = make_run(
        tmp_path,
        [
            base_row(
                description="Available May-Aug near Western.",
                is_sublet="",
                ai_is_sublet="True",
                review_flags='["is_sublet_true_but_ai_confidence_low"]',
                needs_manual_review="True",
            )
        ],
    )
    review_workflow.run_automated_review(run_dir)
    item = current_decisions(run_dir)[0]
    assert item["decision_status"] == "human_review_required"
    assert item["proposed_value"] == "True"
    assert item["reason_code"] == "ai_only_proposal_requires_human"


def test_price_conversion_and_ambiguous_period_preserve_originals(tmp_path: Path) -> None:
    run_dir = make_run(
        tmp_path,
        [
            base_row("1001", price_text="$250 per week", price_numeric="250", price_period="", price_monthly=""),
            base_row("1002", price_text="$900", price_numeric="900", price_period="", price_monthly=""),
        ],
    )
    review_workflow.run_automated_review(run_dir)
    items = {item["listing_id"]: item for item in current_decisions(run_dir)}

    assert items["1001"]["decision_status"] == "auto_resolved"
    assert items["1001"]["reason_code"] == "parser_failure"
    assert items["1001"]["evidence"]["price_classification"] == "parser_failure"
    assert items["1001"]["selected_value"] == 1083.33
    assert items["1001"]["apply_updates"]["price_period"] == "week"
    assert items["1002"]["decision_status"] == "accepted_as_unknown"
    assert items["1002"]["reason_code"] == "period_ambiguous"
    original = read_csv(run_dir / "stage3/canonical.csv")[0]
    assert original["price_text"] == "$250 per week"
    assert original["price_numeric"] == "250"


def test_unknown_and_invalid_prices_are_classified_safely(tmp_path: Path) -> None:
    run_dir = make_run(
        tmp_path,
        [
            base_row("1001", price_text="", price_numeric="", price_period="", price_monthly=""),
            base_row("1002", price_text="$0", price_numeric="0", price_period="month", price_monthly=""),
            base_row("1003", price_text="$500 per semester", price_numeric="500", price_period="semester", price_monthly=""),
            base_row("1004", price_text="$250 per week or $900 per month", price_numeric="250", price_period="", price_monthly=""),
        ],
    )
    review_workflow.run_automated_review(run_dir)
    items = {item["listing_id"]: item for item in current_decisions(run_dir)}
    assert (items["1001"]["decision_status"], items["1001"]["reason_code"]) == (
        "accepted_as_unknown",
        "genuinely_missing",
    )
    assert items["1002"]["reason_code"] == "invalid_price"
    assert (items["1003"]["decision_status"], items["1003"]["reason_code"]) == (
        "accepted_as_unknown",
        "non_monthly_convertible",
    )
    assert items["1004"]["decision_status"] == "human_review_required"
    assert items["1004"]["evidence"]["price_classification"] == (
        "human_review_required"
    )


def test_manual_correction_is_preserved_and_stale_flag_resolves(tmp_path: Path) -> None:
    run_dir = make_run(
        tmp_path,
        [
            base_row(
                is_sublet="False",
                is_sublet_source="manual_review",
                manual_reviewed="True",
                manual_review_note="Not a sublet; option to sublease later only.",
                description="Tenant may sublease later.",
                review_flags='["is_sublet_true_but_ai_confidence_low"]',
                needs_manual_review="True",
            )
        ],
    )
    review_workflow.run_automated_review(run_dir)
    item = current_decisions(run_dir)[0]
    assert item["decision_status"] == "already_resolved"
    assert item["selected_value"] == "False"
    assert item["evidence_source"] == "manual_correction"


def test_conflicting_manual_and_rule_values_require_human(tmp_path: Path) -> None:
    run_dir = make_run(
        tmp_path,
        [
            base_row(
                furnished="False",
                furnished_rule="True",
                furnished_source="manual_review",
                manual_reviewed="True",
                review_flags='["furnished_false_but_description_mentions_furnished"]',
                needs_manual_review="True",
            )
        ],
    )
    review_workflow.run_automated_review(run_dir)
    conflicts = [
        item for item in current_decisions(run_dir) if item["reason_code"] == "manual_and_deterministic_sources_conflict"
    ]
    assert conflicts and conflicts[0]["decision_status"] == "human_review_required"


def test_mock_assisted_proposal_never_auto_resolves(tmp_path: Path) -> None:
    class Proposer:
        def propose(self, row, *, field, reason_code):
            return {
                "proposed_value": "shared",
                "supporting_text": ["bathroom nearby"],
                "conflicting_text": [],
                "reasoning_summary": "Possible shared bathroom; human confirmation needed.",
            }

    run_dir = make_run(
        tmp_path,
        [
            base_row(
                bathroom_type="",
                review_flags='["bathroom_type_low_ai_confidence"]',
                needs_manual_review="True",
            )
        ],
    )
    review_workflow.run_automated_review(run_dir, proposer=Proposer())
    item = current_decisions(run_dir)[0]
    assert item["reviewer_type"] == "codex_assisted"
    assert item["decision_status"] == "human_review_required"
    assert item["proposed_value"] == "shared"
    assert "chain" not in json.dumps(item).casefold()


def cache_row(**updates) -> dict[str, object]:
    row = {
        "geocode_query": "123 Richmond Street, London, ON, Canada",
        "provider": "geoapify",
        "latitude": "43.01",
        "longitude": "-81.27",
        "geocode_status": "ok",
        "geocode_confidence": "0.95",
        "geocode_match_type": "full_match",
        "geocode_result_type": "building",
    }
    row.update(updates)
    return row


@pytest.mark.parametrize(
    ("cache_rows", "row_updates", "reason"),
    [
        ([cache_row(), cache_row()], {}, "geocode_multiple_cached_candidates"),
        ([cache_row(latitude="44")], {"latitude": "44"}, "geocode_outside_london_bounds"),
        ([], {"address": ""}, "geocode_address_query_mismatch"),
    ],
)
def test_unsafe_geocodes_remain_human(
    tmp_path: Path, cache_rows, row_updates, reason
) -> None:
    row = base_row(
        map_ready="False",
        geocode_quality_issue="low_confidence",
        **row_updates,
    )
    run_dir = make_run(tmp_path, [row])
    cache = tmp_path / "cache.csv"
    write_csv(cache, cache_rows, fields=list(cache_row()))
    review_workflow.run_automated_review(run_dir, cache_path=cache)
    item = current_decisions(run_dir)[0]
    assert item["decision_status"] == "human_review_required"
    assert item["reason_code"] == reason


def test_exact_cached_geocode_resolves_stale_map_flag(tmp_path: Path) -> None:
    run_dir = make_run(
        tmp_path,
        [base_row(map_ready="False", geocode_quality_issue="stale_review")],
    )
    cache = tmp_path / "cache.csv"
    write_csv(cache, [cache_row()])
    review_workflow.run_automated_review(run_dir, cache_path=cache)
    item = current_decisions(run_dir)[0]
    assert item["decision_status"] == "auto_resolved"
    assert item["reason_code"] == "cached_geocode_verified"


def test_outputs_group_duplicates_and_are_deterministic(tmp_path: Path) -> None:
    run_dir = make_run(
        tmp_path,
        [
            base_row(
                price_text="$0",
                price_numeric="0",
                price_period="",
                price_monthly="",
                review_flags='["unusual_price_0"]',
                needs_manual_review="True",
            )
        ],
    )
    fixed_time = datetime(2026, 8, 2, tzinfo=timezone.utc)
    first = review_workflow.run_automated_review(run_dir, now=fixed_time)
    before = {
        name: (run_dir / "review" / name).read_bytes()
        for name in review_workflow.REVIEW_OUTPUTS
    }
    second = review_workflow.run_automated_review(run_dir, now=fixed_time)
    after = {
        name: (run_dir / "review" / name).read_bytes()
        for name in review_workflow.REVIEW_OUTPUTS
    }
    remaining = read_csv(run_dir / "review/remaining-human-review.csv")
    assert first == second
    assert before == after
    assert len(remaining) == 1
    assert set(remaining[0]["review_category"].split("|")) >= {
        "ai_review",
        "missing_monthly_price",
    }
    for item in current_decisions(run_dir):
        assert set(review_workflow.REQUIRED_DECISION_FIELDS) <= set(item)


def test_apply_rejects_stale_decisions_and_rolls_back_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = make_run(
        tmp_path,
        [base_row(price_text="$250/week", price_numeric="250", price_period="", price_monthly="")],
    )
    review_workflow.run_automated_review(run_dir)
    canonical = run_dir / "stage3/canonical.csv"
    stale = canonical.read_bytes()
    canonical.write_bytes(stale.replace(b"$250/week", b"$251/week"))
    with pytest.raises(review_workflow.ReviewWorkflowError, match="fingerprint is stale"):
        review_workflow.apply_review_decisions(run_dir)
    canonical.write_bytes(stale)

    protected = [
        run_dir / "stage2/reviewed.csv",
        run_dir / "stage2/review_queue.csv",
        run_dir / "stage3/geocoded.csv",
        run_dir / "stage3/canonical.csv",
        run_dir / "manifest.json",
    ]
    before = {path: path.read_bytes() for path in protected}
    monkeypatch.setattr(
        review_workflow,
        "rebuild_canonical",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("injected")),
    )
    with pytest.raises(RuntimeError, match="injected"):
        review_workflow.apply_review_decisions(run_dir)
    assert {path: path.read_bytes() for path in protected} == before


def test_apply_is_idempotent_rebuilds_and_invalidates_approval(tmp_path: Path) -> None:
    run_dir = make_run(
        tmp_path,
        [
            base_row(
                furnished="False",
                furnished_rule="True",
                furnished_source="ai",
                review_flags='["furnished_false_but_description_mentions_furnished"]',
                needs_manual_review="True",
            )
        ],
    )
    evaluation = evaluate_run_approval(run_dir)
    approve_run(
        run_dir,
        approved_by="fixture-human",
        acknowledge_warnings=bool(evaluation.material_warning_conditions),
    )
    review_workflow.run_automated_review(run_dir)
    first = review_workflow.apply_review_decisions(run_dir)
    canonical = read_csv(run_dir / "stage3/canonical.csv")[0]
    manifest = RunContext.resume(run_dir).manifest

    assert first["applied_decision_count"] >= 1
    assert canonical["furnished"] == "True"
    assert canonical["review_flags"] == "[]"
    assert canonical["needs_manual_review"] == "False"
    assert manifest["canonical_for_import"] is False
    assert manifest["approval"]["status"] == "unapproved"
    assert manifest["approval"]["history"][-1]["by"] == "automated_review_workflow"
    assert (run_dir / "review-index.json").is_file()
    assert (run_dir / "operator-summary.json").is_file()

    second = review_workflow.apply_review_decisions(run_dir)
    assert second["idempotent"] is True
    assert second["applied_decision_count"] == 0


def test_explicit_human_approval_records_reviewer_and_manual_source(
    tmp_path: Path,
) -> None:
    run_dir = make_run(
        tmp_path,
        [
            base_row(
                furnished="",
                ai_furnished="True",
                review_flags='["furnished_ai_evidence_blocked"]',
                needs_manual_review="True",
            )
        ],
    )
    review_workflow.run_automated_review(run_dir)
    records = decisions(run_dir)
    current = next(
        item
        for item in records
        if item["field"] == "furnished"
        and item["decision_status"] == "human_review_required"
    )
    current.update(
        {
            "selected_value": True,
            "human_approved": True,
            "human_reviewer": "fixture-reviewer",
            "human_approved_at_utc": "2026-08-02T01:00:00Z",
        }
    )
    review_workflow._write_jsonl(
        run_dir / "review" / "review-decisions.jsonl", records
    )

    review_workflow.apply_review_decisions(run_dir)
    canonical = read_csv(run_dir / "stage3/canonical.csv")[0]
    assert canonical["furnished"] == "True"
    assert canonical["furnished_source"] == "manual_review"
    assert canonical["manual_reviewed"] == "True"
    assert "fixture-reviewer" in canonical["manual_review_note"]


def test_applied_accepted_unknown_remains_visible_and_preserves_price(
    tmp_path: Path,
) -> None:
    run_dir = make_run(
        tmp_path,
        [base_row(price_text="$900", price_numeric="900", price_period="", price_monthly="")],
    )
    review_workflow.run_automated_review(run_dir)
    result = review_workflow.apply_review_decisions(run_dir)
    canonical = read_csv(run_dir / "stage3/canonical.csv")[0]
    accepted = read_csv(run_dir / "review/accepted-unknown.csv")
    assert result["review_summary"]["pending_safe_application_count"] == 0
    assert canonical["price_text"] == "$900"
    assert canonical["price_numeric"] == "900"
    assert canonical["price_period"] == ""
    assert canonical["price_monthly"] == ""
    assert len(accepted) == 1
    assert accepted[0]["reason_code"] == "period_ambiguous"
    assert result["review_summary"]["accepted_unknowns_documented"] is True


def test_ready_policy_and_staging_commands_never_execute_import(tmp_path: Path) -> None:
    run_dir = make_run(tmp_path, [base_row()])
    summary = review_workflow.run_automated_review(run_dir)
    assert summary["ready_for_approval"] is True
    status = review_workflow.review_status(run_dir)
    assert status["ready_for_approval"] is True
    commands = review_workflow.staging_commands(run_dir.name)
    assert len(commands) == 6
    assert any("approval-status" in command for command in commands)
    assert any("pytest -m postgres" in command for command in commands)
    assert any(
        "database_importer" in command and "$env:TEST_DATABASE_URL" in command
        for command in commands
    )
    assert any("housing_pipeline_runs" in command for command in commands)
    assert all("$env:DATABASE_URL" not in command for command in commands)
    assert RunContext.resume(run_dir).manifest["canonical_for_import"] is False
