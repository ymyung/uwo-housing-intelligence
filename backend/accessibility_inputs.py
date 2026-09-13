"""Validated, bounded inputs and reproducible bundle metadata for the worker."""

from __future__ import annotations

import csv
import hashlib
import json
import tomllib
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from backend.domain import Coordinates, Hotspot, TravelMode
from backend.routing_provider import RoutingGraphMetadata


class AccessibilityInputError(ValueError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_fingerprint(value: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class CoordinateBounds:
    minimum_latitude: float
    maximum_latitude: float
    minimum_longitude: float
    maximum_longitude: float

    def contains(self, coordinates: Coordinates) -> bool:
        return (
            self.minimum_latitude <= coordinates.latitude <= self.maximum_latitude
            and self.minimum_longitude <= coordinates.longitude <= self.maximum_longitude
        )


@dataclass(frozen=True)
class ReviewedProperty:
    property_id: int
    normalized_address: str
    coordinates: Coordinates
    review_status: str

    @property
    def fingerprint(self) -> str:
        return stable_fingerprint(
            {
                "property_id": self.property_id,
                "normalized_address": self.normalized_address,
                "latitude": round(self.coordinates.latitude, 7),
                "longitude": round(self.coordinates.longitude, 7),
                "review_status": self.review_status,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "property_id": self.property_id,
            "normalized_address": self.normalized_address,
            "latitude": self.coordinates.latitude,
            "longitude": self.coordinates.longitude,
            "review_status": self.review_status,
            "origin_fingerprint": self.fingerprint,
        }


@dataclass(frozen=True)
class VerifiedHotspot:
    hotspot: Hotspot
    verification_status: str
    verified_at: datetime

    @property
    def fingerprint(self) -> str:
        coordinates = self.hotspot.coordinates
        return stable_fingerprint(
            {
                "hotspot_id": self.hotspot.id,
                "name": self.hotspot.name,
                "category": self.hotspot.category,
                "latitude": round(coordinates.latitude, 7) if coordinates else None,
                "longitude": round(coordinates.longitude, 7) if coordinates else None,
                "verification_status": self.verification_status,
                "source": self.hotspot.source,
                "verified_at": self.verified_at.isoformat(),
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.hotspot.to_dict(),
            "hotspot_id": self.hotspot.id,
            "verification_status": self.verification_status,
            "verified_at": self.verified_at.isoformat(),
            "hotspot_fingerprint": self.fingerprint,
        }


@dataclass(frozen=True)
class RoutingBundle:
    osm_path: Path
    gtfs_path: Path
    router_config_path: Path
    hotspot_config_path: Path
    build_manifest_path: Path
    metadata: RoutingGraphMetadata
    fingerprints: dict[str, str]


@dataclass(frozen=True)
class AccessibilityWorkerConfig:
    project_root: Path
    run_root: Path
    properties_path: Path
    hotspot_config_path: Path
    osm_path: Path
    gtfs_path: Path
    router_config_path: Path
    build_manifest_path: Path
    routing_provider: str
    otp_base_url: str
    otp_router_id: str
    otp_request_timeout_seconds: float
    provider_profile: str
    database_url_env: str
    persist: bool
    reviewed_only: bool
    property_limit: int
    hotspot_limit: int
    minimum_transit_samples: int
    max_retries: int
    retry_delay_seconds: float
    reference_service_week: date
    bounds: CoordinateBounds


def _table(value: dict[str, Any], name: str) -> dict[str, Any]:
    result = value.get(name)
    if not isinstance(result, dict):
        raise AccessibilityInputError(f"[{name}] must be a TOML table")
    return result


def _resolve(project_root: Path, value: Any, field: str) -> Path:
    if not str(value or "").strip():
        raise AccessibilityInputError(f"{field} is required")
    path = Path(str(value))
    return path if path.is_absolute() else project_root / path


def load_worker_config(path: Path, project_root: Path | None = None) -> AccessibilityWorkerConfig:
    root = (project_root or Path(__file__).resolve().parents[1]).resolve()
    try:
        with path.open("rb") as source:
            payload = tomllib.load(source)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise AccessibilityInputError(f"Could not load worker config: {path}") from exc
    routing = _table(payload, "routing")
    inputs = _table(payload, "inputs")
    selection = _table(payload, "selection")
    persistence = _table(payload, "persistence")
    retries = _table(payload, "retries")
    bounds = _table(payload, "bounds")
    service = _table(payload, "service")
    provider = str(routing.get("provider") or "").strip()
    if provider != "opentripplanner":
        raise AccessibilityInputError("routing.provider must be opentripplanner")
    reference = date.fromisoformat(str(service.get("reference_service_week")))
    if reference.weekday() != 0:
        raise AccessibilityInputError("reference_service_week must be a Monday")
    database_url_env = str(persistence.get("database_url_env") or "").strip()
    if database_url_env == "DATABASE_URL":
        raise AccessibilityInputError(
            "Worker configuration must use a dedicated database URL environment variable"
        )
    config = AccessibilityWorkerConfig(
        project_root=root,
        run_root=_resolve(root, payload.get("run_root"), "run_root"),
        properties_path=_resolve(root, inputs.get("properties"), "inputs.properties"),
        hotspot_config_path=_resolve(root, inputs.get("hotspots"), "inputs.hotspots"),
        osm_path=_resolve(root, inputs.get("osm"), "inputs.osm"),
        gtfs_path=_resolve(root, inputs.get("gtfs"), "inputs.gtfs"),
        router_config_path=_resolve(
            root, inputs.get("router_config"), "inputs.router_config"
        ),
        build_manifest_path=_resolve(
            root, inputs.get("build_manifest"), "inputs.build_manifest"
        ),
        routing_provider=provider,
        otp_base_url=str(routing.get("base_url") or ""),
        otp_router_id=str(routing.get("router_id") or "default"),
        otp_request_timeout_seconds=float(routing.get("request_timeout_seconds", 30)),
        provider_profile=str(routing.get("provider_profile") or "").strip(),
        database_url_env=database_url_env,
        persist=bool(persistence.get("enabled", False)),
        reviewed_only=bool(selection.get("reviewed_only", True)),
        property_limit=int(selection.get("property_limit", 20)),
        hotspot_limit=int(selection.get("hotspot_limit", 3)),
        minimum_transit_samples=int(selection.get("minimum_transit_samples", 2)),
        max_retries=int(retries.get("max_retries", 2)),
        retry_delay_seconds=float(retries.get("initial_delay_seconds", 0.25)),
        reference_service_week=reference,
        bounds=CoordinateBounds(
            float(bounds["minimum_latitude"]),
            float(bounds["maximum_latitude"]),
            float(bounds["minimum_longitude"]),
            float(bounds["maximum_longitude"]),
        ),
    )
    if not config.provider_profile or not config.database_url_env:
        raise AccessibilityInputError(
            "routing.provider_profile and persistence.database_url_env are required"
        )
    if min(config.property_limit, config.hotspot_limit, config.minimum_transit_samples) <= 0:
        raise AccessibilityInputError("Selection limits and sample threshold must be positive")
    if config.max_retries < 0 or config.retry_delay_seconds < 0:
        raise AccessibilityInputError("Retry settings cannot be negative")
    return config


def load_reviewed_properties(
    path: Path,
    *,
    bounds: CoordinateBounds,
    reviewed_only: bool,
    selected_ids: set[int] | None = None,
    limit: int = 20,
    allow_larger_run: bool = False,
) -> list[ReviewedProperty]:
    required = {
        "property_id",
        "latitude",
        "longitude",
        "normalized_address",
        "review_status",
    }
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as source:
            reader = csv.DictReader(source)
            if not reader.fieldnames or not required <= set(reader.fieldnames):
                raise AccessibilityInputError(
                    f"Property CSV must contain: {', '.join(sorted(required))}"
                )
            rows = list(reader)
    except OSError as exc:
        raise AccessibilityInputError(f"Could not load property input: {path}") from exc
    output: list[ReviewedProperty] = []
    seen: set[int] = set()
    for line, row in enumerate(rows, start=2):
        try:
            property_id = int(str(row.get("property_id") or "").strip())
            coordinates = Coordinates(
                float(str(row.get("latitude") or "").strip()),
                float(str(row.get("longitude") or "").strip()),
            )
        except (TypeError, ValueError) as exc:
            raise AccessibilityInputError(
                f"Property CSV line {line} has missing or invalid coordinates"
            ) from exc
        if selected_ids is not None and property_id not in selected_ids:
            continue
        status = str(row.get("review_status") or "").strip().lower()
        address = str(row.get("normalized_address") or "").strip()
        if property_id <= 0 or property_id in seen or not address:
            raise AccessibilityInputError(f"Property CSV line {line} has invalid identity")
        if reviewed_only and status not in {"approved", "reviewed", "verified"}:
            raise AccessibilityInputError(f"Property {property_id} is not reviewed")
        if not bounds.contains(coordinates):
            raise AccessibilityInputError(f"Property {property_id} is outside configured bounds")
        seen.add(property_id)
        output.append(ReviewedProperty(property_id, address, coordinates, status))
    if selected_ids is not None and seen != selected_ids:
        missing = ", ".join(str(value) for value in sorted(selected_ids - seen))
        raise AccessibilityInputError(f"Unknown selected property IDs: {missing}")
    if not output:
        raise AccessibilityInputError("At least one reviewed property is required")
    if len(output) > limit and not allow_larger_run:
        raise AccessibilityInputError(
            f"Selection has {len(output)} properties; safety limit is {limit}"
        )
    return sorted(output, key=lambda item: item.property_id)


def load_verified_hotspots(
    path: Path,
    *,
    bounds: CoordinateBounds,
    selected_ids: set[str] | None = None,
    limit: int = 3,
    allow_larger_run: bool = False,
) -> list[VerifiedHotspot]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AccessibilityInputError(f"Could not load hotspot configuration: {path}") from exc
    rows = payload.get("hotspots") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise AccessibilityInputError("Hotspot configuration must contain a hotspots array")
    output: list[VerifiedHotspot] = []
    known: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise AccessibilityInputError("Each hotspot must be an object")
        hotspot_id = str(row.get("hotspot_id") or "").strip()
        if selected_ids is not None and hotspot_id not in selected_ids:
            continue
        known.add(hotspot_id)
        status = str(row.get("verification_status") or "").strip().lower()
        if not bool(row.get("is_active", True)) or status != "verified":
            raise AccessibilityInputError(f"Hotspot {hotspot_id or '<missing>'} is not verified")
        try:
            coordinates = Coordinates(float(row["latitude"]), float(row["longitude"]))
            verified_at = datetime.fromisoformat(
                str(row["verified_at"]).replace("Z", "+00:00")
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise AccessibilityInputError(f"Hotspot {hotspot_id} is incomplete") from exc
        if not hotspot_id or not str(row.get("source") or "").strip():
            raise AccessibilityInputError("Hotspot identity and provenance are required")
        if verified_at.tzinfo is None or not bounds.contains(coordinates):
            raise AccessibilityInputError(f"Hotspot {hotspot_id} has invalid verification data")
        hotspot = Hotspot(
            id=hotspot_id,
            name=str(row.get("name") or "").strip(),
            coordinates=coordinates,
            address=str(row.get("address") or "").strip() or None,
            category=str(row.get("category") or "other"),
            is_active=True,
            source=str(row["source"]),
        )
        output.append(VerifiedHotspot(hotspot, status, verified_at))
    if selected_ids is not None and known != selected_ids:
        missing = ", ".join(sorted(selected_ids - known))
        raise AccessibilityInputError(f"Unknown selected hotspot IDs: {missing}")
    if not output:
        raise AccessibilityInputError("At least one verified hotspot is required")
    if len(output) > limit and not allow_larger_run:
        raise AccessibilityInputError(
            f"Selection has {len(output)} hotspots; safety limit is {limit}"
        )
    return sorted(output, key=lambda item: item.hotspot.id)


def load_routing_bundle(config: AccessibilityWorkerConfig) -> RoutingBundle:
    paths = {
        "osm_sha256": config.osm_path,
        "gtfs_sha256": config.gtfs_path,
        "router_config_sha256": config.router_config_path,
        "hotspot_config_sha256": config.hotspot_config_path,
    }
    missing = [str(path) for path in paths.values() if not path.is_file()]
    if not config.build_manifest_path.is_file():
        missing.append(str(config.build_manifest_path))
    if missing:
        raise AccessibilityInputError(f"Routing bundle files are missing: {', '.join(missing)}")
    fingerprints = {name: sha256_file(path) for name, path in paths.items()}
    try:
        manifest = json.loads(config.build_manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AccessibilityInputError("Routing build manifest is invalid") from exc
    if not isinstance(manifest, dict):
        raise AccessibilityInputError("Routing build manifest must be an object")
    recorded = manifest.get("input_fingerprints")
    if not isinstance(recorded, dict) or any(recorded.get(key) != value for key, value in fingerprints.items()):
        raise AccessibilityInputError("Routing input fingerprints do not match build manifest")
    try:
        built_at = datetime.fromisoformat(
            str(manifest["graph_built_at"]).replace("Z", "+00:00")
        )
        metadata = RoutingGraphMetadata(
            router_version=str(manifest["router_version"]),
            network_version=str(manifest["network_version"]),
            schedule_version=str(manifest["schedule_version"]),
            graph_built_at=built_at,
        )
    except (KeyError, ValueError) as exc:
        raise AccessibilityInputError("Routing build manifest version metadata is incomplete") from exc
    if built_at.tzinfo is None or any(
        not value.strip()
        for value in (
            metadata.router_version,
            metadata.network_version,
            metadata.schedule_version,
        )
    ):
        raise AccessibilityInputError("Routing build metadata must be versioned and timezone-aware")
    fingerprints["build_manifest_sha256"] = sha256_file(config.build_manifest_path)
    return RoutingBundle(
        config.osm_path,
        config.gtfs_path,
        config.router_config_path,
        config.hotspot_config_path,
        config.build_manifest_path,
        metadata,
        fingerprints,
    )


def parse_modes(value: str) -> tuple[TravelMode, ...]:
    modes: list[TravelMode] = []
    for item in value.split(","):
        try:
            mode = TravelMode(item.strip().lower())
        except ValueError as exc:
            raise AccessibilityInputError(f"Unknown travel mode: {item}") from exc
        if mode is TravelMode.DRIVING:
            raise AccessibilityInputError("Driving is outside the worker scope")
        if mode not in modes:
            modes.append(mode)
    if not modes:
        raise AccessibilityInputError("At least one mode is required")
    return tuple(modes)
