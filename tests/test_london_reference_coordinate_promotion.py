from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from pipeline.reference_data.london.coordinate_promotion import (
    _evidence_changes,
    candidate_evidence,
    execute_disposable_promotion,
    load_frozen_candidate,
    load_promotion_config,
    movement_audit,
)


PRIOR_MANUAL_OVER_100_IDS = {313, 131, 385, 535, 601}
FROZEN_CANDIDATE = (
    Path(__file__).resolve().parents[1]
    / "data"
    / "london-reference-validation"
    / "coordinate-selection-v1"
    / "20260825T021255Z"
    / "coordinate-candidates.csv"
)
requires_private_candidate = pytest.mark.skipif(
    not FROZEN_CANDIDATE.is_file(),
    reason="private generated coordinate candidate is not distributed",
)


@requires_private_candidate
def test_frozen_promotion_input_has_exact_approved_bytes_and_cohort() -> None:
    config = load_promotion_config()
    frozen = load_frozen_candidate(config)

    assert config.version == "coordinate-promotion-v1"
    assert frozen.policy_fingerprint == (
        "1e53391da8c790de34ed4c4db745eed9e8e1402773a96983a9ed1da3b23c429c"
    )
    assert frozen.candidate_fingerprint == (
        "788bd845bf25a9bfc9b79c118f784ff0d830aec9e2e66913fb4e9ec4e11b2e70"
    )
    assert len(frozen.records) == 681
    assert len(frozen.selected) == 218
    assert frozen.decision_counts == {
        "CITY_SELECTED_SHADOW": 218,
        "CONFLICT_REVIEW_SHADOW": 279,
        "GEOAPIFY_RETAINED_SHADOW": 183,
        "NO_USABLE_COORDINATE_SHADOW": 1,
    }
    selected_ids = {int(record["property_id"]) for record in frozen.selected}
    assert selected_ids.isdisjoint(PRIOR_MANUAL_OVER_100_IDS)
    assert max(float(record["movement_meters"]) for record in frozen.selected) < 100


@requires_private_candidate
def test_frozen_input_rejects_policy_or_candidate_fingerprint_drift() -> None:
    config = load_promotion_config()

    with pytest.raises(RuntimeError, match="policy fingerprint mismatch"):
        load_frozen_candidate(
            replace(config, coordinate_selection_policy_fingerprint="0" * 64)
        )
    with pytest.raises(RuntimeError, match="candidate fingerprint mismatch"):
        load_frozen_candidate(replace(config, candidate_fingerprint="0" * 64))


@requires_private_candidate
def test_freshness_payload_identifies_changed_source_evidence() -> None:
    record = load_frozen_candidate(load_promotion_config()).selected[0]
    expected = candidate_evidence(record)
    changed = dict(expected)
    changed["city_dataset_fingerprint"] = "f" * 64
    changed["current_latitude"] = float(changed["current_latitude"]) + 0.001

    differences = _evidence_changes(expected, changed)

    assert set(differences) == {"city_dataset_fingerprint", "current_latitude"}
    assert differences["city_dataset_fingerprint"]["candidate"] != (
        differences["city_dataset_fingerprint"]["current"]
    )


def test_movement_audit_uses_required_buckets_and_percentiles() -> None:
    result = movement_audit([0, 5, 6, 10, 11, 20, 21, 50, 51, 100])

    assert result["count"] == 10
    assert result["median"] == 15.5
    assert result["buckets"] == {
        "0-5": 2,
        "5-10": 2,
        "10-20": 2,
        "20-50": 2,
        "50-100": 2,
        ">100": 0,
    }


class _GuardConnection:
    def execute(self, *_args, **_kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("database access occurred before the explicit guard")


def test_disposable_executor_requires_explicit_test_only_guard() -> None:
    with pytest.raises(RuntimeError, match="explicit test-only guard"):
        execute_disposable_promotion(
            _GuardConnection(),  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            migration_run_id="must-not-run",
        )


def test_promotion_artifacts_are_ignored() -> None:
    gitignore = (Path(__file__).resolve().parents[1] / ".gitignore").read_text(
        encoding="utf-8"
    )
    assert "data/london-reference-validation/" in gitignore
