from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from pipeline import operator, review_ui, review_workflow
from pipeline.operator_config import LocalConfig, OperatorConfig, PipelineConfig, RemoteConfig
from pipeline.remote_executor import CommandResult
from pipeline.run_context import RunContext
from test_review_workflow import base_row, make_run, read_csv


def ui_row(listing_id: str = "1001", **updates):
    row = base_row(
        listing_id,
        description="<script>alert('unsafe')</script> Quiet room near Western.",
        furnished="",
        ai_furnished="True",
        review_flags='["furnished_ai_evidence_blocked"]',
        needs_manual_review="True",
        map_ready="False",
        geocode_confidence="0.45",
        geocode_match_type="partial_match",
        geocode_result_type="street",
        geocode_quality_issue="low_confidence;property_precision_required",
    )
    row.update(updates)
    return row


def make_ui_run(tmp_path: Path, rows=None, *, run_id="review-ui-run") -> Path:
    run_dir = make_run(tmp_path, rows or [ui_row()], run_id=run_id)
    review_workflow.run_automated_review(
        run_dir, now=datetime(2026, 8, 3, tzinfo=timezone.utc)
    )
    return run_dir


def first_issue(run_dir: Path, *, field: str | None = None) -> dict:
    listing = review_ui.load_dashboard(run_dir)["listings"][0]
    return next(
        issue for issue in listing["issues"] if field is None or issue["field"] == field
    )


def decision_payload(
    issue: dict,
    *,
    action="accept_current",
    status="human_approved",
    reviewer_name="Hansen",
    **updates,
) -> dict:
    payload = {
        "base_decision_id": issue["base_decision_id"],
        "listing_id": issue["listing_id"],
        "input_fingerprint": issue["input_fingerprint"],
        "status": status,
        "action": action,
        "reviewer_name": reviewer_name,
        "review_note": "Reviewed against the listing evidence.",
    }
    payload.update(updates)
    return payload


def test_dashboard_loads_one_run_and_groups_overlapping_issues(tmp_path: Path) -> None:
    run_dir = make_ui_run(tmp_path)
    result = review_ui.load_dashboard(run_dir)
    assert result["run_id"] == run_dir.name
    assert result["summary"]["human_review_listings"] == 1
    assert result["summary"]["total_issues"] == 2
    assert len(result["listings"]) == 1
    assert {issue["field"] for issue in result["listings"][0]["issues"]} == {
        "furnished",
        "map_ready",
    }
    assert result["listings"][0]["description"].startswith("<script>")


@pytest.mark.parametrize("run_id", ["../escape", "..", "bad/run", "bad\\run"])
def test_invalid_run_id_and_path_traversal_are_rejected(
    tmp_path: Path, run_id: str
) -> None:
    with pytest.raises(ValueError, match="filesystem-safe"):
        review_ui.resolve_run_directory(run_id, [tmp_path])


def test_missing_artifact_and_mixed_run_ids_fail_clearly(tmp_path: Path) -> None:
    run_dir = make_ui_run(tmp_path)
    decisions = run_dir / "review/review-decisions.jsonl"
    original = decisions.read_bytes()
    decisions.unlink()
    with pytest.raises(review_ui.ReviewUIError, match="missing"):
        review_ui.load_dashboard(run_dir)
    decisions.write_bytes(original)
    summary_path = run_dir / "review/review-auto-summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["run_id"] = "different-run"
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    with pytest.raises(review_ui.ReviewUIError, match="summary run_id"):
        review_ui.load_dashboard(run_dir)


def test_api_loads_static_ui_and_read_only_prevents_writes(tmp_path: Path) -> None:
    run_dir = make_ui_run(tmp_path)
    client = TestClient(review_ui.create_review_app(run_dir, read_only=True))
    page = client.get("/")
    assert page.status_code == 200
    assert "Housing review desk" in page.text
    assert "<script>alert('unsafe')</script>" not in page.text
    state = client.get("/api/state")
    assert state.status_code == 200
    issue = state.json()["listings"][0]["issues"][0]
    response = client.post(
        "/api/decisions", json={"decisions": [decision_payload(issue)]}
    )
    assert response.status_code == 403
    assert "read-only" in response.json()["detail"].casefold()


def test_reviewer_identity_is_required_and_audited(tmp_path: Path) -> None:
    run_dir = make_ui_run(tmp_path)
    issue = first_issue(run_dir, field="furnished")
    with pytest.raises(review_ui.ReviewUIError, match="Reviewer name"):
        review_ui.save_human_decisions(
            run_dir, [decision_payload(issue, reviewer_name="")]
        )
    result = review_ui.save_human_decisions(
        run_dir,
        [decision_payload(issue)],
        now=datetime(2026, 8, 3, 12, tzinfo=timezone.utc),
    )
    assert result["saved_count"] == 1
    saved = next(
        record
        for record in review_workflow._read_jsonl(
            run_dir / "review/review-decisions.jsonl"
        )
        if record.get("reviewer_type") == "human"
    )
    assert saved["reviewer_name"] == "Hansen"
    assert saved["reviewed_at_utc"] == "2026-08-03T12:00:00Z"
    assert saved["human_approved"] is True
    assert set(review_workflow.REQUIRED_DECISION_FIELDS) <= set(saved)


def test_queue_filters_search_and_geocode_marker_data(tmp_path: Path) -> None:
    rows = [
        ui_row("1001", address="123 Richmond Street", geocode_confidence="0.45"),
        ui_row(
            "1002",
            address="999 Outside Road",
            latitude="44.5",
            longitude="-79.0",
            geocode_confidence="0.2",
        ),
    ]
    run_dir = make_ui_run(tmp_path, rows)
    search = review_ui.load_dashboard(run_dir, search="outside")
    assert [item["listing_id"] for item in search["listings"]] == ["1002"]
    filtered = review_ui.load_dashboard(
        run_dir,
        category="geocoding_review",
        field="map_ready",
        maximum_confidence=0.3,
        map_ready=False,
    )
    assert [item["listing_id"] for item in filtered["listings"]] == ["1002"]
    geocode = filtered["listings"][0]["geocoding"]
    assert geocode["latitude"] == 44.5
    assert geocode["longitude"] == -79.0
    assert geocode["inside_london_bounds"] is False
    assert geocode["provider"] == "geoapify"


@pytest.mark.parametrize(
    ("latitude", "longitude", "message"),
    [("not-a-number", "-81", "numeric"), ("91", "-81", "global valid")],
)
def test_invalid_coordinate_input_is_rejected(
    tmp_path: Path, latitude: str, longitude: str, message: str
) -> None:
    run_dir = make_ui_run(tmp_path)
    issue = first_issue(run_dir, field="map_ready")
    payload = decision_payload(
        issue,
        action="correct_geocode",
        latitude=latitude,
        longitude=longitude,
    )
    with pytest.raises(review_ui.ReviewUIError, match=message):
        review_ui.save_human_decisions(run_dir, [payload])


def test_out_of_bounds_coordinates_require_and_record_override_reason(
    tmp_path: Path,
) -> None:
    run_dir = make_ui_run(tmp_path)
    issue = first_issue(run_dir, field="map_ready")
    payload = decision_payload(
        issue,
        action="correct_geocode",
        latitude="44.0",
        longitude="-79.0",
        out_of_bounds_reason="Verified rural property outside normal service bounds.",
    )
    review_ui.save_human_decisions(run_dir, [payload])
    saved = review_ui._active_human_records(
        review_workflow._read_jsonl(run_dir / "review/review-decisions.jsonl")
    )[issue["base_decision_id"]]
    assert saved["evidence"]["inside_london_bounds"] is False
    assert saved["out_of_bounds_reason"].startswith("Verified")


def test_address_correction_preserves_original_and_stales_old_coordinates(
    tmp_path: Path,
) -> None:
    run_dir = make_ui_run(tmp_path)
    issue = first_issue(run_dir, field="map_ready")
    review_ui.save_human_decisions(
        run_dir,
        [
            decision_payload(
                issue,
                action="correct_address",
                corrected_address="456 Oxford Street West",
            )
        ],
    )
    saved = review_ui._active_human_records(
        review_workflow._read_jsonl(run_dir / "review/review-decisions.jsonl")
    )[issue["base_decision_id"]]
    updates = saved["apply_updates"]
    assert updates["address_original"] == "123 Richmond Street"
    assert updates["address"] == "456 Oxford Street West"
    assert updates["latitude"] is None and updates["longitude"] is None
    assert updates["geocode_status"] == "stale_address_correction"
    assert saved["evidence"]["future_geocoding_required"] is True


def test_material_correction_and_exclusion_require_notes(tmp_path: Path) -> None:
    run_dir = make_ui_run(tmp_path)
    issue = first_issue(run_dir, field="furnished")
    correction = decision_payload(
        issue,
        action="correct_value",
        selected_value=True,
        review_note="",
    )
    with pytest.raises(review_ui.ReviewUIError, match="evidence or a note"):
        review_ui.save_human_decisions(run_dir, [correction])
    exclusion = decision_payload(
        issue, action="exclude", status="excluded", review_note=""
    )
    with pytest.raises(review_ui.ReviewUIError, match="Exclusion requires"):
        review_ui.save_human_decisions(run_dir, [exclusion])


def test_draft_is_incomplete_and_accepted_unknown_remains_visible(tmp_path: Path) -> None:
    run_dir = make_ui_run(tmp_path)
    issue = first_issue(run_dir, field="furnished")
    review_ui.save_human_decisions(
        run_dir,
        [
            decision_payload(
                issue,
                action="leave_unresolved",
                status="human_review_required",
                review_note="Need landlord confirmation.",
            )
        ],
    )
    dashboard = review_ui.load_dashboard(run_dir)
    draft = next(
        item for item in dashboard["listings"][0]["issues"] if item["field"] == "furnished"
    )
    assert draft["ui_state"] == "draft"
    assert dashboard["summary"]["completed_listings"] == 0
    payload = decision_payload(
        draft,
        action="accepted_as_unknown",
        status="accepted_as_unknown",
        supersedes_decision_id=draft["human_decision"]["decision_id"],
    )
    review_ui.save_human_decisions(run_dir, [payload])
    updated = review_ui.load_dashboard(run_dir)
    accepted = next(
        item for item in updated["listings"][0]["issues"] if item["field"] == "furnished"
    )
    assert accepted["human_decision"]["human_decision_status"] == "accepted_as_unknown"
    assert any(event["event"] == "human_decision" for event in updated["listings"][0]["history"])


def test_stable_ids_idempotency_and_conflicting_duplicate_protection(
    tmp_path: Path,
) -> None:
    run_dir = make_ui_run(tmp_path)
    issue = first_issue(run_dir, field="furnished")
    payload = decision_payload(issue)
    first = review_ui.save_human_decisions(run_dir, [payload])
    second = review_ui.save_human_decisions(run_dir, [payload])
    assert second["idempotent"] is True
    assert first["saved_decision_ids"][0] in {
        record["decision_id"]
        for record in review_workflow._read_jsonl(
            run_dir / "review/review-decisions.jsonl"
        )
    }
    conflicting = decision_payload(
        issue,
        action="correct_value",
        selected_value=False,
        review_note="Conflicting correction.",
    )
    with pytest.raises(review_ui.ReviewUIError, match="supersedes"):
        review_ui.save_human_decisions(run_dir, [conflicting])


def test_stale_fingerprint_and_failed_batch_leave_file_unchanged(tmp_path: Path) -> None:
    run_dir = make_ui_run(tmp_path, [ui_row("1001"), ui_row("1002")])
    dashboard = review_ui.load_dashboard(run_dir)
    issues = [
        next(issue for issue in listing["issues"] if issue["field"] == "furnished")
        for listing in dashboard["listings"]
    ]
    path = run_dir / "review/review-decisions.jsonl"
    before = path.read_bytes()
    invalid = decision_payload(issues[1], reviewer_name="")
    with pytest.raises(review_ui.ReviewUIError, match="Reviewer name"):
        review_ui.save_human_decisions(
            run_dir, [decision_payload(issues[0]), invalid]
        )
    assert path.read_bytes() == before
    stale = decision_payload(issues[0])
    stale["input_fingerprint"] = "0" * 64
    with pytest.raises(review_ui.ReviewUIError, match="stale"):
        review_ui.save_human_decisions(run_dir, [stale])
    assert path.read_bytes() == before


def test_atomic_writer_failure_preserves_prior_decisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = make_ui_run(tmp_path)
    issue = first_issue(run_dir, field="furnished")
    path = run_dir / "review/review-decisions.jsonl"
    before = path.read_bytes()
    monkeypatch.setattr(
        review_ui,
        "_write_jsonl",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("injected")),
    )
    with pytest.raises(RuntimeError, match="injected"):
        review_ui.save_human_decisions(run_dir, [decision_payload(issue)])
    assert path.read_bytes() == before


def test_bulk_preview_is_homogeneous_and_unsafe_actions_are_unavailable(
    tmp_path: Path,
) -> None:
    run_dir = make_ui_run(tmp_path)
    issues = review_ui.load_dashboard(run_dir)["listings"][0]["issues"]
    geocode = next(issue for issue in issues if issue["field"] == "map_ready")
    unsafe = review_ui.bulk_preview(
        run_dir,
        {"action": "exclude", "base_decision_ids": [geocode["base_decision_id"]]},
    )
    assert unsafe["safe"] is False
    mixed = review_ui.bulk_preview(
        run_dir,
        {
            "action": "accept_current",
            "base_decision_ids": [issue["base_decision_id"] for issue in issues],
        },
    )
    assert mixed["safe"] is False
    assert mixed["affected_listing_count"] == 1
    assert "criteria" in " ".join(mixed).casefold()


def test_html_uses_text_nodes_and_loopback_is_default(tmp_path: Path) -> None:
    run_dir = make_ui_run(tmp_path)
    app = review_ui.create_review_app(run_dir)
    client = TestClient(app)
    script = client.get("/assets/review.js").text
    assert "textContent" in script
    assert "innerHTML" not in script
    assert review_ui.DEFAULT_HOST == "127.0.0.1"
    assert review_ui.validate_bind_host("localhost") == "localhost"
    with pytest.raises(review_ui.ReviewUIError, match="unsafe-development-bind"):
        review_ui.validate_bind_host("0.0.0.0")
    assert (
        review_ui.validate_bind_host(
            "0.0.0.0", unsafe_development_bind=True
        )
        == "0.0.0.0"
    )
    assert client.get("/").headers["x-frame-options"] == "DENY"


def test_manual_geocode_application_uses_existing_audit_and_rebuild_path(
    tmp_path: Path,
) -> None:
    run_dir = make_ui_run(tmp_path)
    issue = first_issue(run_dir, field="map_ready")
    review_ui.save_human_decisions(
        run_dir,
        [decision_payload(issue, action="accept_current_geocode")],
    )
    result = review_workflow.apply_review_decisions(run_dir)
    canonical = read_csv(run_dir / "stage3/canonical.csv")[0]
    assert result["approval_invalidated"] is True
    assert canonical["map_ready"] == "True"
    assert canonical["geocode_manual_override"] == "True"
    assert RunContext.resume(run_dir).manifest["canonical_for_import"] is False


def test_local_and_remote_bundle_review_ui_never_runs_ssh_or_services(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    review_root = tmp_path / "remote-runs"
    run_dir = make_ui_run(review_root, run_id="remote-review-run")
    events = []
    monkeypatch.setattr(
        operator,
        "load_config",
        lambda **kwargs: pytest.fail("remote configuration should not load"),
    )
    monkeypatch.setattr(
        operator,
        "local_preflight",
        lambda *args, **kwargs: pytest.fail("SSH preflight should not run"),
    )
    monkeypatch.setattr(
        operator,
        "serve_review_ui",
        lambda root, **kwargs: events.append((root, kwargs))
        or {
            "ok": True,
            "run_id": root.name,
            "url": "http://127.0.0.1:8765/",
            "read_only": kwargs["read_only"],
            "external_services_used": False,
        },
    )
    args = operator.build_parser().parse_args(
        [
            "review-ui",
            "--remote-host",
            "uwo-server",
            "--review-root",
            str(review_root),
            "--run-id",
            run_dir.name,
            "--reviewer",
            "Hansen",
            "--no-open",
        ]
    )
    exit_code, result = operator.execute(args)
    assert exit_code == 0 and result["external_services_used"] is False
    assert events[0][0] == run_dir.resolve()


def test_remote_sync_checks_canonical_and_uses_explicit_merge_command(
    tmp_path: Path,
) -> None:
    review_root = tmp_path / "reviews"
    run_dir = make_ui_run(review_root, run_id="sync-run")
    config = OperatorConfig(
        RemoteConfig(host="fixture", project_path=r"C:\fixture repo"),
        LocalConfig(review_root),
        PipelineConfig(),
    )

    class Executor:
        def __init__(self):
            self.copies = []
            self.commands = []

        def copy_to(self, local_path, remote_path):
            self.copies.append((local_path, remote_path))
            return CommandResult(0, "", "", 1)

        def run_powershell(self, script, check=True):
            self.commands.append(script)
            if "review-merge-decisions" in script:
                return CommandResult(
                    0,
                    json.dumps(
                        {
                            "ok": True,
                            "run_id": "sync-run",
                            "merged_count": 1,
                            "external_services_used": False,
                        }
                    ),
                    "",
                    1,
                )
            return CommandResult(0, "", "", 1)

    executor = Executor()
    summary = json.loads(
        (run_dir / "review/review-auto-summary.json").read_text(encoding="utf-8")
    )
    result = operator.sync_remote_review_decisions(
        executor,
        config,
        run_id="sync-run",
        snapshot={
            "artifacts": {
                "stage3/canonical.csv": {"sha256": summary["canonical_sha256"]}
            }
        },
    )
    assert result["merged_count"] == 1
    assert len(executor.copies) == 1
    assert any("review-merge-decisions" in command for command in executor.commands)
    assert any("Remove-Item" in command for command in executor.commands)


def test_remote_human_decision_merge_validates_and_is_idempotent(
    tmp_path: Path,
) -> None:
    local = make_ui_run(tmp_path / "local", run_id="shared-run")
    remote = make_ui_run(tmp_path / "remote", run_id="shared-run")
    issue = first_issue(local, field="furnished")
    review_ui.save_human_decisions(local, [decision_payload(issue)])
    incoming = local / "review/review-decisions.jsonl"

    first = review_ui.merge_human_decision_file(remote, incoming)
    second = review_ui.merge_human_decision_file(remote, incoming)
    assert first["merged_count"] == 1
    assert second["idempotent"] is True
    assert len(
        review_ui._active_human_records(
            review_workflow._read_jsonl(remote / "review/review-decisions.jsonl")
        )
    ) == 1


def test_ui_actions_never_execute_approval_database_or_external_services(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = make_ui_run(tmp_path)
    issue = first_issue(run_dir, field="furnished")
    monkeypatch.setattr(
        review_ui,
        "evaluate_run_approval",
        lambda *args, **kwargs: pytest.fail("approval evaluation should not be imported"),
        raising=False,
    )
    before = RunContext.resume(run_dir).manifest
    review_ui.save_human_decisions(run_dir, [decision_payload(issue)])
    after = RunContext.resume(run_dir).manifest
    assert before == after
    assert after["canonical_for_import"] is False
