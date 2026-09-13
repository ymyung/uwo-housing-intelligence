from __future__ import annotations

from backend.listing_history import (
    ChangeOrigin,
    MeaningfulChangeType,
    compare_listing_lifecycle,
    compare_listing_observations,
    project_listing_history,
)


def observation(
    *,
    observed_at: str = "2026-08-01T00:00:00Z",
    title: str | None = "Room for $850",
    description: str | None = "Available September",
    availability_text: str | None = "September",
    change_type: str = "updated",
    **comparison,
):
    return {
        "observed_at": observed_at,
        "title": title,
        "description": description,
        "availability_text": availability_text,
        "change_type": change_type,
        "comparison_data": comparison,
    }


def test_same_normalized_listing_has_no_meaningful_change() -> None:
    previous = observation(price_monthly="850.00", housing_type="Room")
    current = observation(price_monthly=850, housing_type=" room ")

    assert compare_listing_observations(previous, current) == ()


def test_price_change_requires_and_uses_independent_source_evidence() -> None:
    previous = observation(title="Room for $850", price_monthly=850)
    current = observation(title="Room for $800", price_monthly="800.00")

    change = compare_listing_observations(previous, current)[0]

    assert change.change_type is MeaningfulChangeType.PRICE_CHANGED
    assert change.origin is ChangeOrigin.SOURCE_CHANGE
    assert change.previous_value == 850
    assert change.current_value == "800.00"
    assert change.student_visible


def test_availability_change_uses_structured_source_text_evidence() -> None:
    previous = observation(
        availability_text="September",
        availability_category="fall_start",
    )
    current = observation(
        availability_text="Available now",
        availability_category="available_now",
    )

    change = compare_listing_observations(previous, current)[0]

    assert change.change_type is MeaningfulChangeType.AVAILABILITY_CHANGED
    assert change.origin is ChangeOrigin.SOURCE_CHANGE


def test_equivalent_price_serialization_does_not_create_false_change() -> None:
    previous = observation(title="Room", price_monthly="$850")
    current = observation(title="Room", price_monthly="850.00")

    assert compare_listing_observations(previous, current) == ()


def test_provenance_only_change_is_not_a_student_change() -> None:
    previous = {**observation(price_monthly=850), "provenance_data": {"rule": "a"}}
    current = {**observation(price_monthly=850), "provenance_data": {"rule": "b"}}

    assert compare_listing_observations(previous, current) == ()


def test_same_source_evidence_marks_parser_change_as_reinterpretation() -> None:
    previous = observation(lease_type="short_term")
    current = observation(lease_type="standard")

    change = compare_listing_observations(previous, current)[0]

    assert change.change_type is MeaningfulChangeType.LEASE_CHANGED
    assert change.origin is ChangeOrigin.PIPELINE_REINTERPRETATION
    assert not change.student_visible


def test_unknown_to_explicit_without_source_evidence_fails_conservatively() -> None:
    previous = observation(
        title=None,
        description=None,
        availability_text=None,
        furnished=None,
    )
    current = observation(
        title=None,
        description=None,
        availability_text=None,
        furnished=True,
    )

    change = compare_listing_observations(previous, current)[0]

    assert change.change_type is MeaningfulChangeType.FURNISHING_CHANGED
    assert change.origin is ChangeOrigin.UNKNOWN_CHANGE_ORIGIN
    assert not change.student_visible


def test_lifecycle_contract_covers_removal_and_reactivation() -> None:
    removed = compare_listing_lifecycle("possibly_removed", "removed")[0]
    reactivated = compare_listing_lifecycle("removed", "relisted")[0]

    assert removed.change_type is MeaningfulChangeType.LISTING_BECAME_INACTIVE
    assert reactivated.change_type is MeaningfulChangeType.LISTING_REACTIVATED


def test_public_history_is_bounded_ordered_and_omits_ambiguous_changes() -> None:
    rows = [
        observation(
            observed_at="2026-08-01T00:00:00Z",
            title="Room for $850",
            change_type="new",
            price_monthly=850,
        ),
        observation(
            observed_at="2026-08-02T00:00:00Z",
            title="Room for $800",
            price_monthly=800,
        ),
        observation(
            observed_at="2026-08-03T00:00:00Z",
            title="Room for $800",
            price_monthly=800,
            lease_type="standard",
        ),
    ]
    listing = {
        "listing_id": "900001",
        "first_seen_at": "2026-08-01T00:00:00Z",
        "last_seen_at": "2026-08-03T00:00:00Z",
    }

    history = project_listing_history(listing, reversed(rows), event_limit=1)

    assert history["observation_count"] == 3
    assert history["last_meaningful_source_change_at"] == "2026-08-02T00:00:00Z"
    assert [event["type"] for event in history["events"]] == ["PRICE_CHANGED"]
    assert history["events"][0]["previous_value"] == 850
    assert history["events"][0]["current_value"] == 800


def test_relisted_observation_is_a_public_source_event() -> None:
    previous = observation(change_type="unchanged", price_monthly=850)
    current = observation(
        observed_at="2026-08-04T00:00:00Z",
        change_type="relisted",
        price_monthly=850,
    )

    changes = compare_listing_observations(previous, current)

    assert [change.change_type for change in changes] == [
        MeaningfulChangeType.LISTING_REACTIVATED
    ]
    assert changes[0].student_visible
