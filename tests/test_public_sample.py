from pathlib import Path

from fastapi.testclient import TestClient

from backend.main import create_app
from backend.repository import FixtureCsvListingRepository


SAMPLE = Path(__file__).resolve().parents[1] / "data" / "samples" / "listings.demo.csv"


def test_public_sample_is_synthetic_and_api_compatible() -> None:
    repository = FixtureCsvListingRepository(SAMPLE)
    rows = repository.list_summaries()

    assert len(rows) == 3
    assert all(row["listing_url"].startswith("https://example.invalid/") for row in rows)
    assert all("Synthetic" in row["title"] for row in rows)
    assert rows[2]["is_sublet"] is True
    assert rows[2]["location_route_available"] is False
    assert rows[0]["ranking_overall_score"] == 82.0
    assert rows[0]["ranking_explanation"] == {
        "summary": "Synthetic ranking example"
    }

    response = TestClient(create_app(repository=repository)).get("/api/listings")
    assert response.status_code == 200
    assert response.json()["total"] == 3
