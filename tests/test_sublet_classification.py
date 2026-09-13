import pytest

from pipeline.uwo_listing_enricher import UWOListingScraper


@pytest.mark.parametrize(
    "description",
    [
        "Available May–August",
        "Available from May to Aug",
        "Summer availability",
        "Available May 1 to August 31",
        "Four-month summer rental",
        "Normal twelve-month lease beginning in May",
    ],
)
def test_summer_availability_alone_is_not_a_sublet(description: str) -> None:
    result = UWOListingScraper._detect_sublet(None, description, None)

    assert result is None


@pytest.mark.parametrize(
    "description",
    [
        "Sublet available May–August",
        "Looking for someone to take over my lease",
        "Lease takeover",
        "Sublease available",
        "Subletting my room for the summer",
    ],
)
def test_explicit_sublet_language_is_classified_as_a_sublet(description: str) -> None:
    result = UWOListingScraper._detect_sublet(None, description, None)

    assert result is True


def test_empty_sublet_evidence_remains_unresolved() -> None:
    assert UWOListingScraper._detect_sublet(None, None, None) is None


def test_structured_sublet_category_is_explicit_source_evidence() -> None:
    assert UWOListingScraper._detect_sublet(
        None, None, "8", "Sublets"
    ) is True


def test_may_to_august_is_summer_availability_without_becoming_sublet() -> None:
    description = "Rental available May 1 to August 31."

    assert UWOListingScraper._parse_availability_category(
        None, description, None
    ) == "summer_available"
    assert UWOListingScraper._detect_sublet(None, description, None) is None


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("available May 2027 to August 2027", "summer_available"),
        ("available September 2026 through August 2027", "summer_available"),
        ("January 2027 to April 2027", "non_summer"),
        ("January 2027 to June 2027", "summer_available"),
        ("May-Aug", "summer_available"),
        ("September-April", "non_summer"),
        ("September 1 to April 30", "non_summer"),
        ("Available starting September", None),
        ("Available until April", None),
        ("8 month lease", None),
        ("", None),
    ],
)
def test_closed_date_ranges_determine_summer_availability(
    description: str, expected: str | None
) -> None:
    assert UWOListingScraper._parse_availability_category(
        None, description, None
    ) == expected


def test_broughdale_explicit_sublet_range_is_non_summer() -> None:
    description = (
        "I'm looking to sublet/assign the 4th bedroom from September 2026 "
        "to April 2027."
    )

    result = UWOListingScraper._parse_availability_evidence(
        None, description, "Available September 1, 2026"
    )

    assert UWOListingScraper._detect_sublet(None, description, None) is True
    assert result.category == "non_summer"
    assert result.source == "deterministic_description_date_range"
    assert result.evidence == "from September 2026 to April 2027"
    assert (result.start_month, result.end_month) == (9, 4)
    assert (result.start_year, result.end_year) == (2026, 2027)


def test_structured_availability_wins_and_conflict_is_preserved() -> None:
    result = UWOListingScraper._parse_availability_evidence(
        None,
        "Available from September 2026 to April 2027.",
        "Summer availability",
    )

    assert result.category == "summer_available"
    assert result.source == "structured_date_available"
    assert result.conflict is True


@pytest.mark.parametrize(
    "description",
    [
        "September-April $1000/month, May-August $700/month",
        "Winter rate September-April: $1000. Summer rate May-August: $700.",
        "Promotional rate May-August: $650/month",
        (
            "Rent: $3,675/month + utilities (January-April 2026, discounted "
            "winter rate) $3,975/month + utilities (May-December 2026)"
        ),
        (
            "Rent is $1325 due to promotion from September to April and "
            "$1375 from May to August."
        ),
    ],
)
def test_pricing_periods_are_not_availability_evidence(description: str) -> None:
    result = UWOListingScraper._parse_availability_evidence(None, description, None)

    assert result.category is None
    assert result.conflict is False


@pytest.mark.parametrize(
    ("description", "expected"),
    [
        ("Available September-April for $1000/month", "non_summer"),
        ("Lease term September-April. Rent $1000/month.", "non_summer"),
        (
            "Room available September-April. Summer pricing available on request.",
            "non_summer",
        ),
        ("Available May-August. Summer rate $750/month.", "summer_available"),
        (
            "Available September-April. Promotional rate May-August: $700/month.",
            "non_summer",
        ),
    ],
)
def test_availability_context_wins_without_absorbing_separate_pricing_periods(
    description: str, expected: str
) -> None:
    result = UWOListingScraper._parse_availability_evidence(None, description, None)

    assert result.category == expected
    assert result.conflict is False


def test_sublet_range_remains_summer_availability_with_nearby_price() -> None:
    description = "Sublet May-August for $700/month"

    result = UWOListingScraper._parse_availability_evidence(None, description, None)

    assert result.category == "summer_available"
    assert result.conflict is False
    assert UWOListingScraper._detect_sublet(None, description, None) is True


def test_structured_availability_ignores_conflicting_pricing_only_range() -> None:
    result = UWOListingScraper._parse_availability_evidence(
        None,
        "Promotional rate May-August: $650/month.",
        "Available September 2026 to April 2027",
    )

    assert result.category == "non_summer"
    assert result.source == "structured_date_available"
    assert result.conflict is False
