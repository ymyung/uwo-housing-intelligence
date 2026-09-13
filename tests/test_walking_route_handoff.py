from datetime import datetime, timezone

from fastapi.testclient import TestClient

from backend.domain import Coordinates, ProviderMetadata, RouteItinerary, RouteLeg, RouteResult, TravelMode, TravelStatus
from backend.main import create_app
from backend.repository import InMemoryListingRepository
from backend.routing_provider import NoRouteError, TransientRoutingError


class Listings(InMemoryListingRepository):
    def get_property_origin(self, property_id):
        return (43.010161, -81.258333) if property_id == 4 else None


class Routes:
    def __init__(self, failure=None): self.failure, self.request = failure, None
    def get_route(self, request):
        self.request = request
        if self.failure: raise self.failure
        return RouteResult(
            origin=request.origin, destination=request.destination, mode=TravelMode.WALKING,
            distance_meters=1300, duration_seconds=1020, status=TravelStatus.AVAILABLE,
            metadata=ProviderMetadata("otp", "exact_route", datetime.now(timezone.utc)), is_estimate=False,
            itinerary=RouteItinerary((RouteLeg("WALK", 1020, 1300, "_p~iF~ps|U_ulLnnqC_mqNvxq`@", 3),)),
        )


def client(provider):
    return TestClient(create_app(repository=Listings([{"listing_id": "one", "property_id": 4, "latitude": 1, "longitude": 1, "map_ready": True}]), exact_routing_provider=provider))


def test_exact_walking_handoff_uses_canonical_property_origin_and_keeps_click() -> None:
    provider = Routes()
    response = client(provider).post("/api/listings/one/routes/walking", json={"destination_latitude": 43.0096, "destination_longitude": -81.2737})
    assert response.status_code == 200
    assert provider.request.origin == Coordinates(43.010161, -81.258333)
    assert provider.request.destination == Coordinates(43.0096, -81.2737)
    assert provider.request.mode is TravelMode.WALKING
    assert response.json()["route"]["itinerary"]["geometry_available"] is True


def test_handoff_rejects_invalid_or_out_of_graph_clicks_and_returns_safe_failures() -> None:
    app = client(Routes())
    assert app.post("/api/listings/one/routes/walking", json={"destination_latitude": "NaN", "destination_longitude": -81.2}).status_code == 422
    assert app.post("/api/listings/one/routes/walking", json={"destination_latitude": 44, "destination_longitude": -81.2}).status_code == 422
    assert client(Routes(NoRouteError("none"))).post("/api/listings/one/routes/walking", json={"destination_latitude": 43.0096, "destination_longitude": -81.2737}).json()["route"]["status"] == "unavailable"
    assert client(Routes(TransientRoutingError("down"))).post("/api/listings/one/routes/walking", json={"destination_latitude": 43.0096, "destination_longitude": -81.2737}).status_code == 503


def test_handoff_rejects_a_persisted_unavailable_location_before_routing() -> None:
    provider = Routes()
    repository = Listings(
        [{
            "listing_id": "one",
            "property_id": 4,
            "latitude": 43.01,
            "longitude": -81.27,
            "map_ready": True,
            "location_status": "unavailable",
            "location_map_visible": False,
            "location_route_available": False,
        }]
    )
    app = TestClient(
        create_app(repository=repository, exact_routing_provider=provider)
    )

    response = app.post(
        "/api/listings/one/routes/walking",
        json={
            "destination_latitude": 43.0096,
            "destination_longitude": -81.2737,
        },
    )

    assert response.status_code == 422
    assert "unavailable" in response.json()["detail"].lower()
    assert provider.request is None
