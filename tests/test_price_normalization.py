from bs4 import BeautifulSoup
import pandas as pd
import pytest

from pipeline.uwo_listing_enricher import UWOListingScraper, build_website_ready


@pytest.mark.parametrize(
    ("price_context", "expected_period", "expected_monthly"),
    [
        ("$900/month", "month", 900.00),
        ("$300/week", "week", 1300.00),
        ("$50/day", "day", 1520.83),
        ("$1,200 monthly", "month", 1200.00),
        ("$1,200 / month", "month", 1200.00),
        ("$1,200.50 per month", "month", 1200.50),
    ],
)
def test_price_is_parsed_and_normalized_monthly(
    price_context: str, expected_period: str, expected_monthly: float
) -> None:
    soup = BeautifulSoup(f"<h2>123 Huron Street {price_context}</h2>", "html.parser")
    _, _, price_text, price_numeric = UWOListingScraper._parse_title_block(
        UWOListingScraper(), soup
    )
    period = UWOListingScraper._infer_price_period(
        price_context, None, price_numeric
    )

    assert price_text is not None
    assert price_numeric is not None
    assert period == expected_period
    assert UWOListingScraper._normalize_monthly_price(price_numeric, period) == expected_monthly


@pytest.mark.parametrize(
    ("price_context", "expected_text"),
    [
        ("$900/month", "$900/month"),
        ("$300 per week", "$300 per week"),
        ("$1,200 monthly", "$1,200 monthly"),
        ("$850 per bdrm", "$850 per bdrm"),
    ],
)
def test_original_price_text_preserves_the_source_period(
    price_context: str, expected_text: str
) -> None:
    soup = BeautifulSoup(
        f"<h2>123 Huron Street {price_context}</h2>", "html.parser"
    )

    _, _, price_text, _ = UWOListingScraper._parse_title_block(
        UWOListingScraper(), soup
    )

    assert price_text == expected_text


@pytest.mark.parametrize("heading", ["No price supplied", "$not-a-price per month"])
def test_missing_or_unparseable_price_returns_nulls(heading: str) -> None:
    soup = BeautifulSoup(f"<h2>{heading}</h2>", "html.parser")
    _, _, price_text, price_numeric = UWOListingScraper._parse_title_block(
        UWOListingScraper(), soup
    )

    assert price_text is None
    assert price_numeric is None
    assert UWOListingScraper._normalize_monthly_price(price_numeric, "month") is None


def test_unknown_price_period_does_not_invent_monthly_price() -> None:
    assert UWOListingScraper._infer_price_period("$900", None) is None
    assert UWOListingScraper._normalize_monthly_price(900.0, None) is None


def test_description_period_must_refer_to_the_advertised_amount() -> None:
    assert UWOListingScraper._infer_price_period(
        "$950", "Parking is $60 a month.", 950.0
    ) is None
    assert UWOListingScraper._infer_price_period(
        "$1,500", "Rent is $1,600/month with monthly cleaning.", 1500.0
    ) is None


@pytest.mark.parametrize(
    ("price_context", "amount", "description"),
    [
        ("$1,880", 1880.0, "Monthly rent is $1,880 plus utilities."),
        ("$2,500", 2500.0, "Rents from $2,400-$2,600/month."),
        ("$1,795", 1795.0, "Suites starting at,... ($1,695-$2,150) per month."),
        ("$1,880", 1880.0, "Leases of six months receive lower monthly rates."),
        ("$1,880", 1880.0, "Rent the floor or a room; rooms have a higher rent/month."),
    ],
)
def test_explicit_rental_frequency_label_establishes_monthly_period(
    price_context: str, amount: float, description: str,
) -> None:
    assert UWOListingScraper._infer_price_period(
        price_context, description, amount
    ) == "month"


@pytest.mark.parametrize(
    ("title", "description", "amount"),
    [
        ("$1070", "Rent is $1070.00 a month.", 1070.0),
        ("$2700", "The unit is offered at 2700/mo.", 2700.0),
    ],
)
def test_same_amount_explicit_description_period_is_recovered(
    title: str, description: str, amount: float
) -> None:
    assert UWOListingScraper._infer_price_period(
        title, description, amount
    ) == "month"


def test_unknown_period_value_returns_null() -> None:
    assert UWOListingScraper._normalize_monthly_price(900.0, "semester") is None


def test_website_ready_includes_monthly_price_when_available() -> None:
    detail_df = pd.DataFrame(
        [
            {
                "listing_id": "123",
                "source_url": "https://offcampus.uwo.ca/Listings/Details/123",
                "price_numeric": 300.0,
                "price_text": "$300",
                "price_period": "week",
                "price_monthly": 1300.0,
            }
        ]
    )

    result = build_website_ready(detail_df)

    assert result.loc[0, "price_monthly"] == 1300.0


def test_website_ready_accepts_older_rows_without_monthly_price() -> None:
    old_detail_df = pd.DataFrame(
        [
            {
                "listing_id": "123",
                "source_url": "https://offcampus.uwo.ca/Listings/Details/123",
                "price_numeric": 900.0,
                "price_text": "$900",
                "price_period": "month",
            }
        ]
    )

    result = build_website_ready(old_detail_df)

    assert "price_monthly" not in result.columns
    assert result.loc[0, "price_numeric"] == 900.0
