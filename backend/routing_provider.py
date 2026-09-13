"""Provider-neutral adapter for a self-hosted OpenTripPlanner instance."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

import requests

from backend.domain import (
    Coordinates,
    Location,
    ProviderMetadata,
    RouteItinerary,
    RouteLeg,
    RouteRequest,
    RouteResult,
    RouteStop,
    RoutingDiagnostic,
    TravelMode,
    TravelStatus,
    TravelTimeSample,
)
from backend.route_geometry import RouteGeometryError, validate_itinerary_geometry


class RoutingProviderError(RuntimeError):
    """Base error for routing failures safe to expose in worker diagnostics."""

    def __init__(
        self,
        message: str,
        *,
        diagnostics: tuple[RoutingDiagnostic, ...] = (),
    ) -> None:
        super().__init__(message)
        self.diagnostics = diagnostics


class TransientRoutingError(RoutingProviderError):
    """A bounded retry may succeed without changing the request."""


class RoutingValidationError(RoutingProviderError):
    """The request, graph, version, or provider response is invalid."""


class NoRouteError(RoutingProviderError):
    """The provider returned no usable itinerary; this is not transient."""


@dataclass(frozen=True)
class RoutingGraphMetadata:
    router_version: str
    network_version: str
    schedule_version: str
    graph_built_at: datetime

    def to_dict(self) -> dict[str, str]:
        return {
            "router_version": self.router_version,
            "network_version": self.network_version,
            "schedule_version": self.schedule_version,
            "graph_built_at": self.graph_built_at.isoformat(),
        }


class JsonTransport(Protocol):
    def __call__(self, url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]: ...


def requests_json_transport(
    url: str, payload: dict[str, Any], timeout: float
) -> dict[str, Any]:
    try:
        response = requests.post(url, json=payload, timeout=timeout)
        response.raise_for_status()
        parsed = response.json()
    except (requests.Timeout, requests.ConnectionError) as exc:
        raise TransientRoutingError("Routing endpoint is temporarily unavailable") from exc
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status is not None and (status >= 500 or status == 429):
            raise TransientRoutingError(f"Routing endpoint returned HTTP {status}") from exc
        raise RoutingValidationError(f"Routing endpoint returned HTTP {status}") from exc
    except requests.JSONDecodeError as exc:
        raise RoutingValidationError("Routing endpoint returned invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise RoutingValidationError("Routing endpoint JSON must be an object")
    return parsed


def _graphql_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _coordinate(value: Coordinates) -> str:
    return (
        "{ location: { coordinate: { latitude: "
        f"{value.latitude:.7f}, longitude: {value.longitude:.7f}"
        " } } }"
    )


def _mode_input(mode: TravelMode) -> str:
    if mode is TravelMode.WALKING:
        return "{ direct: [WALK] }"
    if mode is TravelMode.CYCLING:
        return "{ direct: [BICYCLE] }"
    if mode is TravelMode.TRANSIT:
        return (
            "{ transitOnly: true, transit: { access: [WALK], egress: [WALK], "
            "transfer: [WALK], transit: [{ mode: BUS }] } }"
        )
    raise RoutingValidationError(f"Unsupported routing mode: {mode.value}")


def _plan_query(request: RouteRequest) -> str:
    departure = request.departure_at or datetime.now(timezone.utc)
    if departure.tzinfo is None:
        raise RoutingValidationError("departure_at must include a timezone")
    return f"""
{{
  planConnection(
    origin: {_coordinate(request.origin)}
    destination: {_coordinate(request.destination)}
    dateTime: {{ earliestDeparture: \"{_graphql_string(departure.isoformat())}\" }}
    modes: {_mode_input(request.mode)}
    first: 5
  ) {{
    routingErrors {{ code description inputField }}
    edges {{ node {{ start end duration
      legs {{ mode duration distance realTime
        start {{ scheduledTime }}
        end {{ scheduledTime }}
        legGeometry {{ points length }}
        route {{ gtfsId shortName longName }}
        from {{ name lat lon stop {{ gtfsId name lat lon }} }}
        to {{ name lat lon stop {{ gtfsId name lat lon }} }}
      }}
    }} }}
  }}
}}
"""


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RoutingValidationError("Routing response contains a non-numeric measure")
    return round(value)


def _parse_time(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # OTP GraphQL commonly returns epoch milliseconds.
        seconds = value / 1000 if value > 10_000_000_000 else value
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise RoutingValidationError("Routing response contains an invalid timestamp") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class RoutingResponse:
    itineraries: tuple[dict[str, Any], ...]
    diagnostics: tuple[RoutingDiagnostic, ...]


def _safe_text(value: Any, *, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _graphql_diagnostics(errors: Any) -> tuple[RoutingDiagnostic, ...]:
    if not isinstance(errors, list):
        return ()
    output: list[RoutingDiagnostic] = []
    for error in errors[:5]:
        if isinstance(error, dict):
            description = _safe_text(error.get("message"), limit=500)
            path = error.get("path")
            input_field = (
                _safe_text(".".join(str(value) for value in path), limit=100)
                if isinstance(path, list)
                else None
            )
        else:
            description = _safe_text(error, limit=500)
            input_field = None
        if description:
            output.append(
                RoutingDiagnostic("invalid_request", description, input_field)
            )
    return tuple(output)


def _routing_diagnostics(connection: dict[str, Any]) -> tuple[RoutingDiagnostic, ...]:
    values = connection.get("routingErrors")
    if not isinstance(values, list):
        return ()
    output: list[RoutingDiagnostic] = []
    for value in values[:10]:
        if not isinstance(value, dict):
            continue
        provider_code = _safe_text(value.get("code"), limit=100)
        description = _safe_text(value.get("description"), limit=500)
        if not provider_code or not description:
            continue
        input_field = _safe_text(value.get("inputField"), limit=100) or None
        code = provider_code.lower()
        if code == "no_transit_connection_in_search_window":
            code = "no_transit_connection"
        output.append(RoutingDiagnostic(code, description, input_field))
    return tuple(output)


def _itineraries(payload: dict[str, Any]) -> RoutingResponse:
    errors = payload.get("errors")
    if errors:
        diagnostics = _graphql_diagnostics(errors)
        raise RoutingValidationError(
            "OpenTripPlanner returned a GraphQL validation error: "
            + "; ".join(value.description for value in diagnostics),
            diagnostics=diagnostics,
        )
    data = payload.get("data")
    connection = data.get("planConnection") if isinstance(data, dict) else None
    if not isinstance(connection, dict):
        raise RoutingValidationError("OpenTripPlanner response schema is invalid")
    diagnostics = _routing_diagnostics(connection)
    edges = connection.get("edges")
    if not isinstance(edges, list):
        raise RoutingValidationError("OpenTripPlanner response schema is invalid")
    output = [edge.get("node") for edge in edges if isinstance(edge, dict)]
    output = [node for node in output if isinstance(node, dict)]
    if not output:
        message = "No route was returned"
        if diagnostics:
            message += ": " + ", ".join(value.code for value in diagnostics)
        raise NoRouteError(message, diagnostics=diagnostics)
    return RoutingResponse(tuple(output), diagnostics)


def _legs(itinerary: dict[str, Any]) -> list[dict[str, Any]]:
    value = itinerary.get("legs")
    if not isinstance(value, list):
        raise RoutingValidationError("Itinerary legs are missing")
    return [leg for leg in value if isinstance(leg, dict)]


def _leg_mode(leg: dict[str, Any]) -> str:
    return str(leg.get("mode") or "").upper()


def _has_transit(itinerary: dict[str, Any]) -> bool:
    return any(_leg_mode(leg) not in {"", "WALK", "BICYCLE"} for leg in _legs(itinerary))


def _sum_leg_measure(legs: list[dict[str, Any]], key: str) -> int | None:
    values = [_as_int(leg.get(key)) for leg in legs]
    present = [value for value in values if value is not None]
    return sum(present) if present else None


def _stop(place: Any) -> RouteStop | None:
    stop = place.get("stop") if isinstance(place, dict) else None
    if not isinstance(stop, dict) or not stop.get("gtfsId") or not stop.get("name"):
        return None
    latitude = stop.get("lat")
    longitude = stop.get("lon")
    coordinates = (
        Coordinates(float(latitude), float(longitude))
        if latitude is not None and longitude is not None
        else None
    )
    return RouteStop(str(stop["gtfsId"]), str(stop["name"]), coordinates)


def _normalized_leg(leg: dict[str, Any]) -> RouteLeg:
    geometry = leg.get("legGeometry")
    points = geometry.get("points") if isinstance(geometry, dict) else None
    point_count = _as_int(geometry.get("length")) if isinstance(geometry, dict) else None
    if not points or point_count is None or point_count < 2:
        raise RoutingValidationError("Routing response is missing usable leg geometry")
    route = leg.get("route")
    start = leg.get("start")
    end = leg.get("end")
    return RouteLeg(
        mode=_leg_mode(leg),
        duration_seconds=_as_int(leg.get("duration")),
        distance_meters=_as_int(leg.get("distance")),
        encoded_polyline=str(points),
        geometry_point_count=point_count,
        route_id=(
            str(route.get("gtfsId"))
            if isinstance(route, dict) and route.get("gtfsId")
            else None
        ),
        route_short_name=(
            str(route.get("shortName"))
            if isinstance(route, dict) and route.get("shortName")
            else None
        ),
        route_long_name=(
            str(route.get("longName"))
            if isinstance(route, dict) and route.get("longName")
            else None
        ),
        from_stop=_stop(leg.get("from")),
        to_stop=_stop(leg.get("to")),
        scheduled_departure_at=_parse_time(
            start.get("scheduledTime") if isinstance(start, dict) else None
        ),
        scheduled_arrival_at=_parse_time(
            end.get("scheduledTime") if isinstance(end, dict) else None
        ),
        is_real_time=leg.get("realTime") is True,
    )


def _normalized_itinerary(itinerary: dict[str, Any]) -> RouteItinerary:
    legs = tuple(_normalized_leg(leg) for leg in _legs(itinerary))
    transit_legs = [leg for leg in legs if leg.mode not in {"WALK", "BICYCLE"}]
    return RouteItinerary(
        legs=legs,
        departure_at=_parse_time(itinerary.get("start")),
        arrival_at=_parse_time(itinerary.get("end")),
        transfer_count=max(0, len(transit_legs) - 1) if transit_legs else None,
        is_live=any(leg.is_real_time for leg in legs),
    )


class OpenTripPlannerProvider:
    """Normalize OTP GraphQL results without retaining provider payloads."""

    provider_name = "opentripplanner"

    def __init__(
        self,
        *,
        base_url: str,
        router_id: str,
        timeout_seconds: float,
        metadata: RoutingGraphMetadata,
        transport: JsonTransport = requests_json_transport,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not base_url.startswith(("http://127.0.0.1", "http://localhost")):
            raise ValueError("Self-hosted routing URL must use localhost or 127.0.0.1")
        if not router_id.strip() or timeout_seconds <= 0:
            raise ValueError("router_id and a positive timeout are required")
        self.endpoint = (
            f"{base_url.rstrip('/')}/otp/gtfs/v1"
        )
        self.router_id = router_id
        self.timeout_seconds = timeout_seconds
        self.metadata = metadata
        self._transport = transport
        self._now = now or (lambda: datetime.now(timezone.utc))

    def preflight(self, expected: RoutingGraphMetadata | None = None) -> RoutingGraphMetadata:
        payload = self._transport(
            self.endpoint, {"query": "{ __typename }"}, self.timeout_seconds
        )
        if payload.get("errors") or "data" not in payload:
            raise RoutingValidationError("OpenTripPlanner health response is invalid")
        if expected and expected != self.metadata:
            raise RoutingValidationError("Routing graph version mismatch")
        return self.metadata

    def _request(self, request: RouteRequest) -> RoutingResponse:
        payload = self._transport(
            self.endpoint,
            {"query": _plan_query(request)},
            self.timeout_seconds,
        )
        return _itineraries(payload)

    def get_route(self, request: RouteRequest) -> RouteResult:
        if request.mode not in {TravelMode.WALKING, TravelMode.CYCLING}:
            raise RoutingValidationError("get_route supports walking and cycling only")
        response = self._request(request)
        selected = next(
            (item for item in response.itineraries if not _has_transit(item)),
            response.itineraries[0],
        )
        legs = _legs(selected)
        provider_modes = {_leg_mode(leg) for leg in legs}
        expected = "WALK" if request.mode is TravelMode.WALKING else "BICYCLE"
        if provider_modes - {expected}:
            raise RoutingValidationError("Provider response mode does not match request")
        duration = _as_int(selected.get("duration"))
        distance = _sum_leg_measure(legs, "distance")
        itinerary = _normalized_itinerary(selected)
        try:
            validate_itinerary_geometry(
                itinerary,
                origin=request.origin,
                destination=request.destination,
            )
        except RouteGeometryError as exc:
            raise RoutingValidationError(f"Invalid route geometry: {exc}") from exc
        return RouteResult(
            origin=request.origin,
            destination=request.destination,
            mode=request.mode,
            distance_meters=distance,
            duration_seconds=duration,
            status=TravelStatus.AVAILABLE,
            metadata=ProviderMetadata(
                provider=self.provider_name,
                calculation_type="exact_route",
                calculated_at=self._now(),
            ),
            is_estimate=False,
            confidence=1.0,
            provider_mode=expected.lower(),
            itinerary=itinerary,
        )

    def get_samples(
        self,
        origin: Location,
        destination: Location,
        departures: list[datetime],
    ) -> list[TravelTimeSample]:
        if origin.coordinates is None or destination.coordinates is None:
            raise RoutingValidationError("Transit origin and destination require coordinates")
        samples: list[TravelTimeSample] = []
        for departure in departures:
            request = RouteRequest(
                origin.coordinates,
                destination.coordinates,
                TravelMode.TRANSIT,
                departure,
            )
            try:
                response = self._request(request)
            except NoRouteError as exc:
                diagnostics = exc.diagnostics or (
                    RoutingDiagnostic("no_route", "No route was returned."),
                )
                samples.append(
                    TravelTimeSample(
                        departure_at=departure,
                        duration_seconds=None,
                        status=TravelStatus.UNAVAILABLE,
                        provider=self.provider_name,
                        schedule_version=self.metadata.schedule_version,
                        network_version=self.metadata.network_version,
                        routing_diagnostics=diagnostics,
                    )
                )
                continue
            selected = next(
                (item for item in response.itineraries if _has_transit(item)), None
            )
            if selected is None:
                diagnostics = response.diagnostics or (
                    RoutingDiagnostic(
                        "no_transit_itinerary",
                        "The provider returned no itinerary containing transit.",
                    ),
                )
                samples.append(
                    TravelTimeSample(
                        departure_at=departure,
                        duration_seconds=None,
                        status=TravelStatus.UNAVAILABLE,
                        provider=self.provider_name,
                        schedule_version=self.metadata.schedule_version,
                        network_version=self.metadata.network_version,
                        routing_diagnostics=diagnostics,
                    )
                )
                continue
            legs = _legs(selected)
            itinerary = _normalized_itinerary(selected)
            try:
                validate_itinerary_geometry(
                    itinerary,
                    origin=origin.coordinates,
                    destination=destination.coordinates,
                )
            except RouteGeometryError as exc:
                raise RoutingValidationError(f"Invalid route geometry: {exc}") from exc
            walking = [leg for leg in legs if _leg_mode(leg) == "WALK"]
            transit = [leg for leg in legs if _leg_mode(leg) not in {"", "WALK"}]
            routes: list[str] = []
            for leg in transit:
                route = leg.get("route")
                if isinstance(route, dict):
                    route_id = route.get("gtfsId") or route.get("shortName")
                    if route_id:
                        routes.append(str(route_id))
            first_stop = transit[0].get("from") if transit else None
            last_stop = transit[-1].get("to") if transit else None
            origin_stop = first_stop.get("stop") if isinstance(first_stop, dict) else None
            destination_stop = last_stop.get("stop") if isinstance(last_stop, dict) else None
            duration = _as_int(selected.get("duration"))
            walking_duration = _sum_leg_measure(walking, "duration")
            in_vehicle = _sum_leg_measure(transit, "duration")
            arrival = _parse_time(selected.get("end"))
            waiting = (
                max(0, duration - (walking_duration or 0) - (in_vehicle or 0))
                if duration is not None
                else None
            )
            samples.append(
                TravelTimeSample(
                    departure_at=departure,
                    arrival_at=arrival,
                    duration_seconds=duration,
                    walking_duration_seconds=walking_duration,
                    waiting_duration_seconds=waiting,
                    in_vehicle_duration_seconds=in_vehicle,
                    transfer_count=max(0, len(transit) - 1),
                    distance_meters=_sum_leg_measure(legs, "distance"),
                    origin_stop_id=(
                        str(origin_stop.get("gtfsId"))
                        if isinstance(origin_stop, dict) and origin_stop.get("gtfsId")
                        else None
                    ),
                    destination_stop_id=(
                        str(destination_stop.get("gtfsId"))
                        if isinstance(destination_stop, dict)
                        and destination_stop.get("gtfsId")
                        else None
                    ),
                    route_ids=tuple(dict.fromkeys(routes)),
                    status=TravelStatus.AVAILABLE,
                    provider=self.provider_name,
                    schedule_version=self.metadata.schedule_version,
                    network_version=self.metadata.network_version,
                    routing_diagnostics=response.diagnostics,
                    itinerary=itinerary,
                )
            )
        return samples


class FixtureAccessibilityRoutingProvider:
    """Deterministic worker provider; tests control results and failures."""

    provider_name = "fixture"

    def __init__(
        self,
        metadata: RoutingGraphMetadata,
        *,
        routes: dict[TravelMode, RouteResult] | None = None,
        samples: dict[datetime, TravelTimeSample] | None = None,
        failures: list[Exception] | None = None,
    ) -> None:
        self.metadata = metadata
        self.routes = dict(routes or {})
        self.samples = dict(samples or {})
        self.failures = list(failures or [])
        self.calls: list[tuple[str, Any]] = []

    def _fail(self) -> None:
        if self.failures:
            raise self.failures.pop(0)

    def preflight(self, expected: RoutingGraphMetadata | None = None) -> RoutingGraphMetadata:
        self.calls.append(("preflight", None))
        self._fail()
        if expected and expected != self.metadata:
            raise RoutingValidationError("Routing graph version mismatch")
        return self.metadata

    def get_route(self, request: RouteRequest) -> RouteResult:
        self.calls.append(("route", request))
        self._fail()
        try:
            result = self.routes[request.mode]
        except KeyError as exc:
            raise NoRouteError("No fixture route") from exc
        return RouteResult(
            **{
                **result.__dict__,
                "origin": request.origin,
                "destination": request.destination,
                "mode": request.mode,
            }
        )

    def get_samples(
        self,
        origin: Location,
        destination: Location,
        departures: list[datetime],
    ) -> list[TravelTimeSample]:
        self.calls.append(("samples", tuple(departures)))
        self._fail()
        del origin, destination
        return [self.samples[value] for value in departures if value in self.samples]
