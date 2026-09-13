from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys

import pytest

from pipeline import run_context
from pipeline import database_importer
from pipeline.database_importer import (
    ExistingState,
    InMemoryImportDatabase,
    ImportValidationError,
    build_import_plan,
    load_and_validate_run,
    import_configuration,
    print_change_summary,
)
from pipeline.run_approval import (
    APPROVAL_VERSION,
    APPROVED,
    UNAPPROVED,
    RunApprovalError,
    approval_manifest_fingerprint,
    approval_validation_errors,
    approve_run,
    evaluate_run_approval,
    sha256_file,
    unapprove_run,
)
from pipeline.run_context import RunContext


FIXED_TIME = datetime(2026, 7, 30, 19, 30, tzinfo=timezone.utc)


def _unapproved_run(make_stage4_run, rows=None, **kwargs) -> Path:
    selected_rows = (
        [make_stage4_run.listing_row("100")] if rows is None else rows
    )
    return make_stage4_run(
        selected_rows, canonical_approved=False, **kwargs
    )


def _approve(run_dir: Path, **kwargs):
    return approve_run(
        run_dir,
        approved_by=kwargs.pop("approved_by", "fixture-reviewer"),
        note=kwargs.pop("note", "Reviewed synthetic queues"),
        now=kwargs.pop("now", FIXED_TIME),
        **kwargs,
    )


def test_valid_completed_run_is_approval_eligible(make_stage4_run) -> None:
    run_dir = _unapproved_run(make_stage4_run)

    evaluation = evaluate_run_approval(run_dir)

    assert evaluation.blocking_conditions == ()
    assert evaluation.approval_status == UNAPPROVED
    assert not evaluation.canonical_for_import
    assert evaluation.summary.discovered_listings == 1
    assert evaluation.summary.canonical_rows == 1


def test_resolved_stage_error_history_does_not_block_approval(make_stage4_run) -> None:
    run_dir = _unapproved_run(make_stage4_run, run_id="resolved-error-approval")
    context = RunContext.resume(run_dir)
    previous = context.manifest["stages"]["stage3"]
    context.start_stage("stage3")
    context.fail_stage("stage3", RuntimeError("temporary fixture failure"))
    context.start_stage("stage3")
    context.finish_stage(
        "stage3",
        input_rows=previous["input_rows"],
        output_rows=previous["output_rows"],
        metrics=previous.get("metrics"),
    )
    run_context.finalize_run(context)

    evaluation = evaluate_run_approval(run_dir)
    persisted = RunContext.resume(run_dir).manifest
    assert not any("fatal errors" in item for item in evaluation.blocking_conditions)
    assert persisted["errors"] == []
    assert persisted["error_history"][0]["resolved_at_utc"] is not None


def test_legacy_resolved_errors_are_auditable_but_not_approval_blockers(
    make_stage4_run,
) -> None:
    run_dir = _unapproved_run(make_stage4_run, run_id="legacy-resolved-approval")
    context = RunContext.resume(run_dir)
    context.manifest["errors"] = [
        {
            "stage": "stage3",
            "type": "RuntimeError",
            "message": "Missing GEOAPIFY_API_KEY",
            "at_utc": f"2026-08-01T00:00:0{index}Z",
        }
        for index in range(3)
    ]
    context.manifest["error_history"] = None
    context.save()

    run_context.finalize_run(context)
    evaluation = evaluate_run_approval(run_dir)
    persisted = RunContext.resume(run_dir).manifest
    assert persisted["errors"] == []
    assert len(persisted["error_history"]) == 3
    assert all(item["legacy_source"] for item in persisted["error_history"])
    assert not any("fatal errors" in item for item in evaluation.blocking_conditions)


def test_approve_without_confirm_prints_summary_and_makes_no_change(
    make_stage4_run, monkeypatch, capsys
) -> None:
    run_dir = _unapproved_run(make_stage4_run)
    before = (run_dir / "manifest.json").read_bytes()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_context",
            "approve",
            "--run-dir",
            str(run_dir),
            "--approved-by",
            "fixture-reviewer",
            "--note",
            "Reviewed synthetic queues",
        ],
    )

    run_context.main()

    assert (run_dir / "manifest.json").read_bytes() == before
    output = capsys.readouterr().out
    assert "review_summary:" in output
    assert "rerun with --confirm" in output


def test_approve_with_confirmation_records_auditable_utc_metadata(
    make_stage4_run
) -> None:
    run_dir = _unapproved_run(make_stage4_run)

    manifest = _approve(
        run_dir, approved_by="fixture-reviewer", note="Reviewed AI and geocode queues"
    )

    approval = manifest["approval"]
    assert manifest["canonical_for_import"] is True
    assert approval["status"] == APPROVED
    assert approval["approval_version"] == APPROVAL_VERSION
    assert approval["approved_by"] == "fixture-reviewer"
    assert approval["note"] == "Reviewed AI and geocode queues"
    assert approval["approved_at_utc"] == "2026-07-30T19:30:00Z"
    parsed = datetime.fromisoformat(approval["approved_at_utc"].replace("Z", "+00:00"))
    assert parsed.tzinfo is not None and parsed.utcoffset().total_seconds() == 0
    assert approval["canonical_csv_fingerprint"] == sha256_file(
        run_dir / "stage3" / "canonical.csv"
    )
    assert approval["manifest_fingerprint"] == approval_manifest_fingerprint(manifest)
    assert approval["history"][0]["event"] == "approved"
    assert approval["warnings_acknowledged"] is False


def test_approval_requires_actor_but_note_is_optional(make_stage4_run) -> None:
    run_dir = _unapproved_run(make_stage4_run)

    with pytest.raises(RunApprovalError, match="approved_by is required"):
        approve_run(run_dir, approved_by="   ")

    manifest = _approve(run_dir, note=None)
    assert manifest["approval"]["note"] is None
    assert approval_validation_errors(
        manifest, run_dir / "stage3" / "canonical.csv"
    ) == ()

    second_run = _unapproved_run(make_stage4_run, run_id="clean-extra-ack")
    extra_ack = _approve(second_run, acknowledge_warnings=True)
    assert extra_ack["approval"]["warnings_acknowledged"] is False
    assert approval_validation_errors(
        extra_ack, second_run / "stage3" / "canonical.csv"
    ) == ()


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"root_status": "failed"}, "Run status"),
        ({"root_status": "running"}, "Run status"),
        ({"recorded_rows": 2}, "row count"),
        ({"max_pages": 1}, "smoke-test"),
        ({"stage1_limit": 1}, "row limit"),
        (
            {"stage0_warning": "Discovery count fell substantially from fixture."},
            "incomplete discovery",
        ),
    ],
)
def test_known_ineligible_runs_are_blocked(
    make_stage4_run, kwargs: dict[str, object], expected: str
) -> None:
    run_dir = _unapproved_run(make_stage4_run, **kwargs)

    evaluation = evaluate_run_approval(run_dir)

    assert any(expected.casefold() in item.casefold() for item in evaluation.blocking_conditions)
    with pytest.raises(RunApprovalError, match="not approval-eligible"):
        _approve(run_dir)


def test_missing_canonical_csv_blocks_approval(make_stage4_run) -> None:
    run_dir = _unapproved_run(make_stage4_run)
    (run_dir / "stage3" / "canonical.csv").unlink()

    evaluation = evaluate_run_approval(run_dir)

    assert any("canonical" in item.casefold() and "missing" in item.casefold()
               for item in evaluation.blocking_conditions)


def test_duplicate_and_missing_source_ids_block_approval(make_stage4_run) -> None:
    duplicate = _unapproved_run(
        make_stage4_run,
        [
            make_stage4_run.listing_row("100"),
            make_stage4_run.listing_row("100", title="Duplicate fixture"),
        ],
        stage0_ids=["100", "200"],
    )
    missing_row = make_stage4_run.listing_row("100")
    missing_row["listing_id"] = ""
    missing = _unapproved_run(
        make_stage4_run, [missing_row], stage0_ids=["100"]
    )

    duplicate_evaluation = evaluate_run_approval(duplicate)
    missing_evaluation = evaluate_run_approval(missing)

    assert any("duplicate source listing ID" in item
               for item in duplicate_evaluation.blocking_conditions)
    assert any("missing or invalid source listing ID" in item
               for item in missing_evaluation.blocking_conditions)


def test_zero_listing_run_blocks_approval(make_stage4_run) -> None:
    run_dir = _unapproved_run(make_stage4_run, [], stage0_ids=[])

    evaluation = evaluate_run_approval(run_dir)

    assert any("zero listings" in item.casefold() for item in evaluation.blocking_conditions)


def test_unsupported_skipped_stage3_and_missing_stage_record_are_blocked(
    make_stage4_run
) -> None:
    skipped_dir = _unapproved_run(make_stage4_run, run_id="skipped-stage3")
    skipped = RunContext.resume(skipped_dir)
    skipped.manifest["stages"]["stage3"]["status"] = "skipped"
    skipped.save()
    missing_dir = _unapproved_run(make_stage4_run, run_id="missing-stage")
    missing = RunContext.resume(missing_dir)
    del missing.manifest["stages"]["stage3_qc"]
    missing.save()

    assert any("stage3" in item for item in evaluate_run_approval(skipped_dir).blocking_conditions)
    assert any("stage3_qc" in item for item in evaluate_run_approval(missing_dir).blocking_conditions)


def test_nonfatal_warning_is_visible_but_permits_human_approval(
    make_stage4_run
) -> None:
    run_dir = _unapproved_run(
        make_stage4_run,
        stage0_warning="Discovery stopped after detecting a repeated result page.",
    )

    evaluation = evaluate_run_approval(run_dir)

    assert evaluation.blocking_conditions == ()
    assert any("repeated result page" in warning for warning in evaluation.warnings)
    assert evaluation.material_warning_conditions
    with pytest.raises(RunApprovalError, match="acknowledge_warnings"):
        _approve(run_dir)
    approved = _approve(run_dir, acknowledge_warnings=True)
    assert approved["canonical_for_import"] is True
    assert approved["approval"]["warnings_acknowledged"] is True
    assert approved["approval"]["acknowledged_warning_conditions"]
    assert approved["approval"]["history"][-1]["warnings_acknowledged"] is True


def test_warning_approval_cli_requires_and_records_acknowledgement(
    make_stage4_run, monkeypatch
) -> None:
    run_dir = _unapproved_run(
        make_stage4_run,
        stage0_warning="Discovery stopped after detecting a repeated result page.",
    )
    base_args = [
        "run_context",
        "approve",
        "--run-dir",
        str(run_dir),
        "--approved-by",
        "fixture-reviewer",
        "--confirm",
    ]
    monkeypatch.setattr(sys, "argv", base_args)
    with pytest.raises(SystemExit, match="--acknowledge-warnings"):
        run_context.main()

    monkeypatch.setattr(sys, "argv", base_args + ["--acknowledge-warnings"])
    run_context.main()
    approval = RunContext.resume(run_dir).manifest["approval"]
    assert approval["warnings_acknowledged"] is True


def test_fatal_manifest_errors_cannot_be_acknowledged_away(
    make_stage4_run,
) -> None:
    run_dir = _unapproved_run(make_stage4_run)
    context = RunContext.resume(run_dir)
    context.manifest["errors"] = [
        {"stage": "stage1", "type": "FixtureError", "message": "synthetic fatal"}
    ]
    context.save()

    evaluation = evaluate_run_approval(run_dir)
    assert "Manifest contains fatal errors" in evaluation.blocking_conditions
    with pytest.raises(RunApprovalError, match="not approval-eligible"):
        _approve(run_dir, acknowledge_warnings=True)


def test_review_summary_uses_available_manifest_and_canonical_fields(
    make_stage4_run
) -> None:
    rows = [
        make_stage4_run.listing_row("100", price_monthly="800"),
        make_stage4_run.listing_row(
            "200",
            scraped_ok="false",
            ai_error="synthetic AI failure",
            needs_manual_review="true",
            manual_reviewed="false",
            address="",
            geocode_status="not_found",
            geocode_confidence="0.4",
            geocode_quality_issue="low_confidence",
            latitude="",
            longitude="",
            map_ready="false",
            price_monthly="25",
            review_flags='["consensus_disagreement_is_sublet"]',
        ),
        make_stage4_run.listing_row("300", price_monthly=""),
    ]
    run_dir = _unapproved_run(make_stage4_run, rows)

    summary = evaluate_run_approval(run_dir).summary

    assert summary.discovered_listings == 3
    assert summary.canonical_rows == 3
    assert summary.stage1_failures == 1
    assert summary.ai_review_rows == 1
    assert summary.unresolved_manual_review_rows == 1
    assert summary.ai_errors == 1
    assert summary.missing_addresses == 1
    assert summary.geocode_failures == 1
    assert summary.low_confidence_geocodes == 1
    assert summary.geocode_review_rows == 1
    assert summary.map_ready_rows == 2
    assert summary.not_map_ready_rows == 1
    assert summary.suspicious_price_rows == 1
    assert summary.missing_monthly_price_rows == 1
    assert summary.sublet_review_rows == 1
    assert summary.manifest_warnings == 0


def test_unavailable_summary_metrics_are_not_invented(make_stage4_run) -> None:
    run_dir = _unapproved_run(make_stage4_run)

    summary = evaluate_run_approval(run_dir).summary

    assert summary.unresolved_manual_review_rows is None


def test_legacy_manifest_is_unapproved_but_can_be_approved(make_stage4_run) -> None:
    run_dir = _unapproved_run(make_stage4_run)
    context = RunContext.resume(run_dir)
    context.manifest.pop("approval", None)
    context.manifest.pop("canonical_for_import", None)
    context.save()

    evaluation = evaluate_run_approval(run_dir)

    assert evaluation.approval_status == UNAPPROVED
    assert not evaluation.canonical_for_import
    assert evaluation.blocking_conditions == ()
    assert _approve(run_dir)["approval"]["status"] == APPROVED


def test_reapproval_preserves_history_and_updates_canonical_fingerprint(
    make_stage4_run
) -> None:
    run_dir = _unapproved_run(make_stage4_run)
    first = _approve(run_dir)
    previous_fingerprint = first["approval"]["canonical_csv_fingerprint"]
    canonical = run_dir / "stage3" / "canonical.csv"
    canonical.write_text(
        canonical.read_text(encoding="utf-8").replace("$800 per month", "$825 per month"),
        encoding="utf-8",
    )

    second = _approve(
        run_dir,
        approved_by="second-reviewer",
        note="Reviewed changed synthetic canonical row",
        now=datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc),
    )

    approval = second["approval"]
    assert approval["canonical_csv_fingerprint"] != previous_fingerprint
    assert approval["approved_by"] == "second-reviewer"
    assert [event["event"] for event in approval["history"]] == [
        "approved",
        "reapproved",
    ]
    assert approval["history"][1]["previous_status"] == APPROVED
    assert (
        approval["history"][1]["previous_canonical_csv_fingerprint"]
        == previous_fingerprint
    )


def test_unapproval_preserves_prior_approval_and_requires_reason(
    make_stage4_run
) -> None:
    run_dir = _unapproved_run(make_stage4_run)
    approved = _approve(run_dir)

    with pytest.raises(RunApprovalError, match="unapproved_by is required"):
        unapprove_run(run_dir, unapproved_by=" ", reason="Synthetic reason")
    with pytest.raises(RunApprovalError, match="reason is required"):
        unapprove_run(run_dir, unapproved_by="fixture-reviewer", reason="")
    unapproved = unapprove_run(
        run_dir,
        unapproved_by="fixture-reviewer",
        reason="Synthetic quality issue discovered",
        now=datetime(2026, 7, 31, 13, 0, tzinfo=timezone.utc),
    )

    approval = unapproved["approval"]
    assert unapproved["canonical_for_import"] is False
    assert approval["status"] == UNAPPROVED
    assert approval["approved_by"] == approved["approval"]["approved_by"]
    assert approval["manifest_fingerprint"] == approved["approval"]["manifest_fingerprint"]
    assert approval["unapproved_by"] == "fixture-reviewer"
    assert approval["reason"] == "Synthetic quality issue discovered"
    assert approval["history"][-1]["event"] == UNAPPROVED
    with pytest.raises(RunApprovalError, match="already unapproved"):
        unapprove_run(
            run_dir, unapproved_by="fixture-reviewer", reason="Repeated request"
        )


def test_canonical_and_manifest_tampering_invalidate_approval_and_import(
    make_stage4_run
) -> None:
    canonical_dir = _unapproved_run(make_stage4_run, run_id="canonical-tamper")
    _approve(canonical_dir)
    canonical = canonical_dir / "stage3" / "canonical.csv"
    canonical.write_text(
        canonical.read_text(encoding="utf-8").replace("$800 per month", "$850 per month"),
        encoding="utf-8",
    )
    canonical_evaluation = evaluate_run_approval(canonical_dir)
    assert canonical_evaluation.canonical_fingerprint_valid is False
    with pytest.raises(ImportValidationError, match="canonical CSV fingerprint"):
        load_and_validate_run(canonical_dir)

    manifest_dir = _unapproved_run(make_stage4_run, run_id="manifest-tamper")
    _approve(manifest_dir)
    context = RunContext.resume(manifest_dir)
    context.manifest["configuration"]["stage0"]["max_pages"] = 99
    context.save()
    manifest_evaluation = evaluate_run_approval(manifest_dir)
    assert manifest_evaluation.manifest_fingerprint_valid is False
    with pytest.raises(ImportValidationError, match="manifest fingerprint"):
        load_and_validate_run(manifest_dir)


def test_approval_metadata_and_updated_timestamp_do_not_self_invalidate(
    make_stage4_run
) -> None:
    run_dir = _unapproved_run(make_stage4_run)
    _approve(run_dir)
    context = RunContext.resume(run_dir)
    before = context.manifest["approval"]["manifest_fingerprint"]
    context.manifest["approval"]["note"] = "Updated audit note only"
    context.save()

    assert approval_manifest_fingerprint(context.manifest) == before
    assert approval_validation_errors(
        context.manifest, context.paths.stage3_canonical
    ) == ()


def test_warning_acknowledgement_metadata_cannot_be_cleared_after_approval(
    make_stage4_run,
) -> None:
    run_dir = _unapproved_run(
        make_stage4_run,
        stage0_warning="Discovery stopped after detecting a repeated result page.",
    )
    approved = _approve(run_dir, acknowledge_warnings=True)
    fingerprint = approved["approval"]["manifest_fingerprint"]
    context = RunContext.resume(run_dir)
    context.manifest["approval"]["warnings_acknowledged"] = False
    context.save()

    assert approval_manifest_fingerprint(context.manifest) == fingerprint
    assert "material warnings were not acknowledged" in approval_validation_errors(
        context.manifest, context.paths.stage3_canonical
    )


def test_importer_accepts_approved_rejects_unapproved_and_records_override(
    make_stage4_run, capsys
) -> None:
    approved_dir = _unapproved_run(make_stage4_run, run_id="approved-import")
    _approve(approved_dir)
    approved = load_and_validate_run(approved_dir)
    assert not approved.override_used

    unapproved_dir = _unapproved_run(make_stage4_run, run_id="override-import")
    with pytest.raises(ImportValidationError, match="not approved"):
        load_and_validate_run(unapproved_dir)
    overridden = load_and_validate_run(
        unapproved_dir,
        allow_noncanonical_run=True,
        override_reason="Synthetic recovery exercise",
    )
    assert overridden.override_used
    assert overridden.override_reason == "Synthetic recovery exercise"
    assert overridden.manifest.get("canonical_for_import") is False
    plan = build_import_plan(overridden, ExistingState.empty())
    print_change_summary(plan, dry_run=True)
    assert "exceptional noncanonical override" in capsys.readouterr().err
    assert import_configuration(
        missing_run_threshold=2,
        skip_lifecycle_updates=False,
        override_used=True,
        override_reason=overridden.override_reason,
    )["noncanonical_override"]["reason"] == "Synthetic recovery exercise"


def test_noncanonical_override_requires_reason_and_never_approves_run(
    make_stage4_run,
) -> None:
    run_dir = _unapproved_run(make_stage4_run)
    before = (run_dir / "manifest.json").read_bytes()

    with pytest.raises(ImportValidationError, match="override_reason is required"):
        load_and_validate_run(run_dir, allow_noncanonical_run=True)

    overridden = load_and_validate_run(
        run_dir,
        allow_noncanonical_run=True,
        override_reason="Reviewed exceptional recovery",
    )
    assert overridden.override_used is True
    assert (run_dir / "manifest.json").read_bytes() == before


def test_approved_dry_run_does_not_change_manifest(
    make_stage4_run, monkeypatch
) -> None:
    run_dir = _unapproved_run(make_stage4_run)
    _approve(run_dir)
    before = (run_dir / "manifest.json").read_bytes()
    monkeypatch.setattr(
        sys,
        "argv",
        ["database_importer", "--run-dir", str(run_dir), "--dry-run"],
    )

    database_importer.main()

    assert (run_dir / "manifest.json").read_bytes() == before


def test_post_import_unapproval_and_reapproval_do_not_replace_imported_run(
    make_stage4_run,
) -> None:
    run_dir = _unapproved_run(make_stage4_run)
    _approve(run_dir)
    database = InMemoryImportDatabase()
    initial = load_and_validate_run(run_dir)
    database.import_run(initial)

    unapprove_run(
        run_dir,
        unapproved_by="fixture-reviewer",
        reason="Additional review required",
    )
    assert "100" in database.state.listings_by_source_id
    _approve(run_dir, note="Additional review completed")
    reapproved = load_and_validate_run(run_dir)

    with pytest.raises(ImportValidationError, match="different content"):
        database.import_run(reapproved)
    assert "100" in database.state.listings_by_source_id


def test_unapproved_run_is_rejected_until_reapproved(make_stage4_run) -> None:
    run_dir = _unapproved_run(make_stage4_run)
    _approve(run_dir)
    unapprove_run(
        run_dir, unapproved_by="fixture-reviewer", reason="Synthetic revocation"
    )
    with pytest.raises(ImportValidationError, match="not approved"):
        load_and_validate_run(run_dir)
    _approve(run_dir, note="Synthetic issue reviewed again")
    assert load_and_validate_run(run_dir).override_used is False


def test_stale_manifest_and_canonical_review_snapshots_are_rejected(
    make_stage4_run
) -> None:
    manifest_dir = _unapproved_run(make_stage4_run, run_id="stale-manifest")
    manifest_evaluation = evaluate_run_approval(manifest_dir)
    context = RunContext.resume(manifest_dir)
    context.manifest["command"] = ["changed-after-review"]
    context.save()
    with pytest.raises(RunApprovalError, match="Manifest changed"):
        _approve(
            manifest_dir,
            expected_manifest_file_fingerprint=(
                manifest_evaluation.manifest_file_fingerprint
            ),
        )

    canonical_dir = _unapproved_run(make_stage4_run, run_id="stale-canonical")
    canonical_evaluation = evaluate_run_approval(canonical_dir)
    canonical = canonical_dir / "stage3" / "canonical.csv"
    canonical.write_text(
        canonical.read_text(encoding="utf-8").replace("$800", "$801"),
        encoding="utf-8",
    )
    with pytest.raises(RunApprovalError, match="Canonical CSV changed"):
        _approve(
            canonical_dir,
            expected_canonical_csv_fingerprint=(
                canonical_evaluation.canonical_csv_fingerprint
            ),
        )


def test_approval_status_is_read_only_and_safe_for_invalid_manifest(
    make_stage4_run, monkeypatch, capsys, tmp_path
) -> None:
    run_dir = _unapproved_run(make_stage4_run)
    before = (run_dir / "manifest.json").read_bytes()
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_context", "approval-status", "--run-dir", str(run_dir)],
    )
    run_context.main()
    assert (run_dir / "manifest.json").read_bytes() == before
    output = capsys.readouterr().out
    assert "approval_status: unapproved" in output
    assert "canonical_fingerprint_valid: unavailable" in output
    assert "approval_history_events: 0" in output
    assert "material_warning_conditions:" in output

    invalid = tmp_path / "invalid-run"
    invalid.mkdir()
    (invalid / "manifest.json").write_text("{invalid", encoding="utf-8")
    evaluation = evaluate_run_approval(invalid)
    assert "manifest.json is invalid" in evaluation.blocking_conditions


def test_cli_confirm_and_unapprove_commands(make_stage4_run, monkeypatch) -> None:
    run_dir = _unapproved_run(make_stage4_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_context",
            "approve",
            "--run-dir",
            str(run_dir),
            "--approved-by",
            "fixture-reviewer",
            "--note",
            "Reviewed synthetic queues",
            "--confirm",
        ],
    )
    run_context.main()
    assert RunContext.resume(run_dir).manifest["canonical_for_import"] is True

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_context",
            "unapprove",
            "--run-dir",
            str(run_dir),
            "--changed-by",
            "fixture-reviewer",
            "--note",
            "Synthetic issue discovered",
        ],
    )
    run_context.main()
    reloaded = RunContext.resume(run_dir).manifest
    assert reloaded["canonical_for_import"] is False
    assert reloaded["approval"]["status"] == UNAPPROVED
    json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))


def test_unsupported_approval_version_is_rejected_by_importer(
    make_stage4_run,
) -> None:
    run_dir = _unapproved_run(make_stage4_run)
    _approve(run_dir)
    context = RunContext.resume(run_dir)
    context.manifest["approval"]["approval_version"] = 999
    context.save()

    with pytest.raises(ImportValidationError, match="approval version"):
        load_and_validate_run(run_dir)


def test_importer_cli_requires_override_reason(
    make_stage4_run, monkeypatch
) -> None:
    run_dir = _unapproved_run(make_stage4_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "database_importer",
            "--run-dir",
            str(run_dir),
            "--dry-run",
            "--allow-noncanonical-run",
        ],
    )

    with pytest.raises(SystemExit, match="--override-reason is required"):
        database_importer.main()
