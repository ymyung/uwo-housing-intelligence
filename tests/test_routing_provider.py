from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backend.domain import Coordinates, Location, RouteRequest, TravelMode, TravelStatus
from backend.routing_provider import (
    NoRouteError,
    OpenTripPlannerProvider,
    RoutingGraphMetadata,
    RoutingValidationError,
    TransientRoutingError,
    _mode_input,
    _plan_query,
)


NOW = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)
METADATA = RoutingGraphMetadata(
    "otp-fixture", "network-fixture", "schedule-fixture", NOW
)
ORIGIN = Coordinates(43.0, -81.25)
DESTINATION = Coordinates(43.0096, -81.2737)


def encoded(points: list[Coordinates]) -> str:
    output = []
    previous = [0, 0]
    for point in points:
        for index, value in enumerate((point.latitude, point.longitude)):
            current = round(value * 100_000)
            delta = current - previous[index]
            previous[index] = current
            encoded_value = ~(delta << 1) if delta < 0 else delta << 1
            while encoded_value >= 0x20:
                output.append(chr((0x20 | (encoded_value & 0x1F)) + 63))
                encoded_value >>= 5
            output.append(chr(encoded_value + 63))
    return "".join(output)


def itinerary(mode: str = "WALK") -> dict:
    return {
        "data": {
            "planConnection": {
                "edges": [
                    {
                        "node": {
                            "start": "2026-09-14T08:00:00-04:00",
                            "end": "2026-09-14T08:20:00-04:00",
                            "duration": 1200,
                            "distance": 1500,
                            "legs": [
                                {
                                    "mode": mode,
                                    "duration": 1200,
                                    "distance": 1500,
                                    "start": {"scheduledTime": "2026-09-14T08:00:00-04:00"},
                                    "end": {"scheduledTime": "2026-09-14T08:20:00-04:00"},
                                    "realTime": False,
                                    "legGeometry": {
                                        "points": encoded([ORIGIN, DESTINATION]),
                                        "length": 2,
                                    },
                                    "from": {"name": "Origin"},
                                    "to": {"name": "Destination"},
                                }
                            ],
                        }
                    }
                ]
            }
        }
    }


def transit_itinerary() -> dict:
    stop = Coordinates(43.001, -81.251)
    return {
        "data": {
            "planConnection": {
                "edges": [
                    {
                        "node": {
                            "start": "2026-09-14T08:00:00-04:00",
                            "end": "2026-09-14T08:30:00-04:00",
                            "duration": 1800,
                            "distance": 6100,
                            "legs": [
                                {
                                    "mode": "WALK",
                                    "duration": 300,
                                    "distance": 350,
                                    "start": {"scheduledTime": "2026-09-14T08:00:00-04:00"},
                                    "end": {"scheduledTime": "2026-09-14T08:05:00-04:00"},
                                    "realTime": False,
                                    "legGeometry": {
                                        "points": encoded([ORIGIN, stop]),
                                        "length": 2,
                                    },
                                },
                                {
                                    "mode": "BUS",
                                    "duration": 1200,
                                    "distance": 5500,
                                    "route": {"gtfsId": "LTC:13", "shortName": "13"},
                                    "start": {"scheduledTime": "2026-09-14T08:10:00-04:00"},
                                    "end": {"scheduledTime": "2026-09-14T08:30:00-04:00"},
                                    "realTime": False,
                                    "legGeometry": {
                                        "points": encoded([stop, DESTINATION]),
                                        "length": 2,
                                    },
                                    "from": {"stop": {"gtfsId": "LTC:100", "name": "Board stop", "lat": stop.latitude, "lon": stop.longitude}},
                                    "to": {"stop": {"gtfsId": "LTC:200", "name": "Exit stop", "lat": DESTINATION.latitude, "lon": DESTINATION.longitude}},
                                },
                            ],
                        }
                    }
                ]
            }
        }
    }


def provider(transport) -> OpenTripPlannerProvider:
    return OpenTripPlannerProvider(
        base_url="http://127.0.0.1:8080",
        router_id="default",
        timeout_seconds=3,
        metadata=METADATA,
        transport=transport,
        now=lambda: NOW,
    )


def test_preflight_and_version_mismatch_are_explicit() -> None:
    client = provider(lambda *_: {"data": {"__typename": "Query"}})
    assert client.preflight(METADATA) == METADATA
    different = RoutingGraphMetadata("other", "network-fixture", "schedule-fixture", NOW)
    with pytest.raises(RoutingValidationError, match="version mismatch"):
        client.preflight(different)


def test_mode_inputs_preserve_direct_modes_and_require_transit() -> None:
    assert _mode_input(TravelMode.WALKING) == "{ direct: [WALK] }"
    assert _mode_input(TravelMode.CYCLING) == "{ direct: [BICYCLE] }"
    transit = _mode_input(TravelMode.TRANSIT)
    assert "transitOnly: true" in transit
    assert "access: [WALK]" in transit
    assert "egress: [WALK]" in transit
    assert "transfer: [WALK]" in transit
    assert "transit: [{ mode: BUS }]" in transit
    assert "direct:" not in transit


def test_plan_query_requests_only_schema_supported_routing_fields() -> None:
    query = _plan_query(
        RouteRequest(ORIGIN, DESTINATION, TravelMode.TRANSIT, NOW)
    )
    assert "routingErrors { code description inputField }" in query
    assert "legs { mode duration distance realTime" in query
    assert "legGeometry { points length }" in query
    assert "scheduledTime" in query
    assert "legs { mode duration distance start" not in query
    assert "node { start end duration distance" not in query


@pytest.mark.parametrize(
    ("mode", "provider_mode"),
    [(TravelMode.WALKING, "walk"), (TravelMode.CYCLING, "bicycle")],
)
def test_walking_and_cycling_are_normalized(mode, provider_mode) -> None:
    payload = itinerary("WALK" if mode is TravelMode.WALKING else "BICYCLE")
    result = provider(lambda *_: payload).get_route(
        RouteRequest(ORIGIN, DESTINATION, mode)
    )
    assert result.duration_seconds == 1200
    assert result.distance_meters == 1500
    assert result.provider_mode == provider_mode
    assert "planConnection" not in result.to_dict()
    assert result.geometry is None
    assert result.itinerary is not None
    assert result.itinerary.has_geometry is True


def test_transit_samples_normalize_nullable_components_and_provenance() -> None:
    departures = [
        datetime(2026, 9, 14, hour, tzinfo=timezone.utc) for hour in (11, 12, 13)
    ]
    client = provider(lambda *_: transit_itinerary())
    samples = client.get_samples(
        Location("p", "Property", ORIGIN),
        Location("h", "Campus", DESTINATION),
        departures,
    )
    assert len(samples) == 3
    assert samples[0].walking_duration_seconds == 300
    assert samples[0].in_vehicle_duration_seconds == 1200
    assert samples[0].waiting_duration_seconds == 300
    assert samples[0].route_ids == ("LTC:13",)
    assert samples[0].origin_stop_id == "LTC:100"
    assert samples[0].schedule_version == "schedule-fixture"
    assert samples[0].itinerary is not None
    assert samples[0].itinerary.transit_legs[0].route_short_name == "13"
    assert samples[0].itinerary.transit_legs[0].from_stop.name == "Board stop"


def test_no_route_and_invalid_response_are_non_transient() -> None:
    with pytest.raises(NoRouteError):
        provider(lambda *_: {"data": {"planConnection": {"edges": []}}}).get_route(
            RouteRequest(ORIGIN, DESTINATION, TravelMode.WALKING)
        )
    with pytest.raises(RoutingValidationError, match="schema"):
        provider(lambda *_: {"data": {}}).get_route(
            RouteRequest(ORIGIN, DESTINATION, TravelMode.WALKING)
        )


def test_graphql_validation_errors_expose_only_sanitized_messages() -> None:
    payload = {
        "errors": [
            {
                "message": "Cannot query field invalidField",
                "path": ["planConnection", "invalidField"],
                "locations": [{"line": 99, "column": 1}],
                "extensions": {"raw": "must-not-leak"},
            }
        ]
    }
    with pytest.raises(RoutingValidationError) as captured:
        provider(lambda *_: payload).get_route(
            RouteRequest(ORIGIN, DESTINATION, TravelMode.WALKING)
        )
    diagnostic = captured.value.diagnostics[0]
    assert diagnostic.code == "invalid_request"
    assert diagnostic.input_field == "planConnection.invalidField"
    assert "invalidField" in diagnostic.description
    assert "must-not-leak" not in str(captured.value)


@pytest.mark.parametrize(
    ("provider_code", "normalized_code"),
    [
        ("OUTSIDE_SERVICE_PERIOD", "outside_service_period"),
        ("NO_STOPS_IN_RANGE", "no_stops_in_range"),
        ("NO_TRANSIT_CONNECTION", "no_transit_connection"),
    ],
)
def test_empty_transit_edges_preserve_normalized_routing_errors(
    provider_code, normalized_code
) -> None:
    payload = {
        "data": {
            "planConnection": {
                "edges": [],
                "routingErrors": [
                    {
                        "code": provider_code,
                        "description": f"Fixture {provider_code}",
                        "inputField": "DATE_TIME",
                        "raw": "must-not-leak",
                    }
                ],
            }
        }
    }
    sample = provider(lambda *_: payload).get_samples(
        Location("p", "Property", ORIGIN),
        Location("h", "Campus", DESTINATION),
        [NOW],
    )[0]
    assert sample.status is TravelStatus.UNAVAILABLE
    assert sample.duration_seconds is None
    assert sample.routing_diagnostics[0].code == normalized_code
    assert sample.routing_diagnostics[0].input_field == "DATE_TIME"
    assert "raw" not in sample.to_dict()["routing_diagnostics"][0]


def test_transit_no_route_is_a_normalized_null_sample_not_zero() -> None:
    departure = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)
    samples = provider(
        lambda *_: {"data": {"planConnection": {"edges": []}}}
    ).get_samples(
        Location("p", "Property", ORIGIN),
        Location("h", "Campus", DESTINATION),
        [departure],
    )
    assert len(samples) == 1
    assert samples[0].status is TravelStatus.UNAVAILABLE
    assert samples[0].duration_seconds is None
    assert samples[0].routing_diagnostics[0].code == "no_route"


def test_direct_walking_itinerary_is_not_accepted_as_transit() -> None:
    sample = provider(lambda *_: itinerary("WALK")).get_samples(
        Location("p", "Property", ORIGIN),
        Location("h", "Campus", DESTINATION),
        [NOW],
    )[0]
    assert sample.status is TravelStatus.UNAVAILABLE
    assert sample.duration_seconds is None
    assert sample.routing_diagnostics[0].code == "no_transit_itinerary"


def test_transport_timeout_is_preserved_for_bounded_retry_classification() -> None:
    def timeout(*_):
        raise TransientRoutingError("fixture timeout")

    with pytest.raises(TransientRoutingError, match="timeout"):
        provider(timeout).get_route(
            RouteRequest(ORIGIN, DESTINATION, TravelMode.WALKING)
        )


def test_adapter_refuses_nonlocal_endpoint_and_wrong_provider_mode() -> None:
    with pytest.raises(ValueError, match="localhost"):
        OpenTripPlannerProvider(
            base_url="https://hosted.example",
            router_id="default",
            timeout_seconds=3,
            metadata=METADATA,
        )
    with pytest.raises(RoutingValidationError, match="mode"):
        provider(lambda *_: itinerary("BUS")).get_route(
            RouteRequest(ORIGIN, DESTINATION, TravelMode.WALKING)
        )
