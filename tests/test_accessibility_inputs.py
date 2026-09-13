from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from backend.accessibility_inputs import (
    AccessibilityInputError,
    AccessibilityWorkerConfig,
    CoordinateBounds,
    ReviewedProperty,
    RoutingBundle,
    VerifiedHotspot,
    load_routing_bundle,
    load_worker_config,
    sha256_file,
)
from backend.accessibility_repository import InMemoryAccessibilityRepository
from backend.accessibility_runs import AccessibilityRunStore
from backend.domain import (
    Coordinates,
    Hotspot,
    ProviderMetadata,
    RouteResult,
    TravelMode,
    TravelStatus,
)
from backend.routing_provider import FixtureAccessibilityRoutingProvider, RoutingGraphMetadata
from scripts import run_accessibility_worker as cli


GRAPH = RoutingGraphMetadata(
    "otp-test", "network-v1", "schedule-v1", datetime(2026, 9, 1, tzinfo=timezone.utc)
)

PROPERTY = ReviewedProperty(
    10, "123 Test Street, London, Ontario", Coordinates(43.0, -81.25), "approved"
)
HOTSPOT = VerifiedHotspot(
    Hotspot(
        "western-main-campus",
        "Western main campus",
        Coordinates(43.0096, -81.2737),
        category="campus",
        source="fixture",
    ),
    "verified",
    datetime(2026, 9, 1, tzinfo=timezone.utc),
)


def worker_route(mode: TravelMode) -> RouteResult:
    return RouteResult(
        PROPERTY.coordinates,
        HOTSPOT.hotspot.coordinates,
        mode,
        2400,
        1800 if mode is TravelMode.WALKING else 600,
        TravelStatus.AVAILABLE,
        ProviderMetadata("fixture", "exact_route", GRAPH.graph_built_at),
        False,
        1.0,
        provider_mode="walk" if mode is TravelMode.WALKING else "bicycle",
    )


def config(tmp_path: Path) -> AccessibilityWorkerConfig:
    return AccessibilityWorkerConfig(
        project_root=tmp_path,
        run_root=tmp_path / "runs",
        properties_path=tmp_path / "properties.csv",
        hotspot_config_path=tmp_path / "hotspots.json",
        osm_path=tmp_path / "network.osm.pbf",
        gtfs_path=tmp_path / "transit.gtfs.zip",
        router_config_path=tmp_path / "router-config.json",
        build_manifest_path=tmp_path / "build-manifest.json",
        routing_provider="opentripplanner",
        otp_base_url="http://127.0.0.1:8080",
        otp_router_id="default",
        otp_request_timeout_seconds=3,
        provider_profile="fixture-v1",
        database_url_env="TEST_DATABASE_URL",
        persist=False,
        reviewed_only=True,
        property_limit=20,
        hotspot_limit=3,
        minimum_transit_samples=2,
        max_retries=2,
        retry_delay_seconds=0,
        reference_service_week=date(2026, 9, 14),
        bounds=CoordinateBounds(42.8, 43.2, -81.5, -80.9),
    )


def build_bundle(tmp_path: Path) -> tuple[AccessibilityWorkerConfig, RoutingBundle]:
    value = config(tmp_path)
    for path, content in (
        (value.osm_path, b"osm fixture"),
        (value.gtfs_path, b"gtfs fixture"),
        (value.router_config_path, b"{}"),
        (value.hotspot_config_path, b'{"hotspots": []}'),
    ):
        path.write_bytes(content)
    fingerprints = {
        "osm_sha256": sha256_file(value.osm_path),
        "gtfs_sha256": sha256_file(value.gtfs_path),
        "router_config_sha256": sha256_file(value.router_config_path),
        "hotspot_config_sha256": sha256_file(value.hotspot_config_path),
    }
    value.build_manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "router_version": GRAPH.router_version,
                "network_version": GRAPH.network_version,
                "schedule_version": GRAPH.schedule_version,
                "graph_built_at": GRAPH.graph_built_at.isoformat(),
                "input_fingerprints": fingerprints,
            }
        ),
        encoding="utf-8",
    )
    return value, load_routing_bundle(value)


def test_bundle_uses_exact_byte_fingerprints_and_rejects_changed_content(
    tmp_path: Path,
) -> None:
    value, bundle = build_bundle(tmp_path)
    assert bundle.metadata == GRAPH
    assert bundle.fingerprints["osm_sha256"] == sha256_file(value.osm_path)
    value.osm_path.write_bytes(b"changed")
    with pytest.raises(AccessibilityInputError, match="fingerprints"):
        load_routing_bundle(value)


def test_config_requires_monday_and_dedicated_database_environment(tmp_path: Path) -> None:
    text = (Path("config/accessibility-worker.example.toml").read_text(encoding="utf-8"))
    bad_env = tmp_path / "bad-env.toml"
    bad_env.write_text(text.replace("ACCESSIBILITY_DATABASE_URL", "DATABASE_URL"), encoding="utf-8")
    with pytest.raises(AccessibilityInputError, match="dedicated"):
        load_worker_config(bad_env)
    bad_week = tmp_path / "bad-week.toml"
    bad_week.write_text(text.replace("2026-09-14", "2026-09-15"), encoding="utf-8")
    with pytest.raises(AccessibilityInputError, match="Monday"):
        load_worker_config(bad_week)


def test_cli_dry_run_performs_no_provider_calls_writes_or_run_artifacts(
    tmp_path: Path, monkeypatch
) -> None:
    value, bundle = build_bundle(tmp_path)
    fixture = FixtureAccessibilityRoutingProvider(GRAPH)
    repository = InMemoryAccessibilityRepository()
    monkeypatch.setattr(cli, "load_worker_config", lambda _: value)
    monkeypatch.setattr(cli, "load_routing_bundle", lambda _: bundle)
    monkeypatch.setattr(
        cli,
        "_selection",
        lambda *_args, **_kwargs: (value.properties_path, [PROPERTY], [HOTSPOT]),
    )
    monkeypatch.setattr(cli, "_repository", lambda _: repository)
    monkeypatch.setattr(cli, "_provider", lambda *_: fixture)
    args = argparse.Namespace(
        config=str(tmp_path / "config.toml"),
        properties=None,
        property_ids=None,
        hotspot_ids=None,
        allow_larger_run=False,
        modes="walking,cycling,transit",
        dry_run=True,
    )
    result = cli._run(args, resume=False)
    assert result["status"] == "dry_run"
    assert result["routing_provider_calls_made"] == 0
    assert result["database_writes_made"] == 0
    assert fixture.calls == []
    assert repository.profiles == []
    assert not value.run_root.exists()


def test_cli_resume_skips_completed_units_after_interruption(
    tmp_path: Path, monkeypatch
) -> None:
    value, bundle = build_bundle(tmp_path)
    repository = InMemoryAccessibilityRepository()
    first_provider = FixtureAccessibilityRoutingProvider(
        GRAPH,
        routes={
            TravelMode.WALKING: worker_route(TravelMode.WALKING),
            TravelMode.CYCLING: worker_route(TravelMode.CYCLING),
        },
    )
    original_route = first_provider.get_route
    call_count = 0

    def interrupt_second(request):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise KeyboardInterrupt("fixture interruption")
        return original_route(request)

    first_provider.get_route = interrupt_second
    monkeypatch.setattr(cli, "load_worker_config", lambda _: value)
    monkeypatch.setattr(cli, "load_routing_bundle", lambda _: bundle)
    monkeypatch.setattr(
        cli,
        "_selection",
        lambda *_args, **_kwargs: (value.properties_path, [PROPERTY], [HOTSPOT]),
    )
    monkeypatch.setattr(cli, "_repository", lambda _: repository)
    monkeypatch.setattr(cli, "_provider", lambda *_: first_provider)
    run_args = argparse.Namespace(
        config=str(tmp_path / "config.toml"),
        properties=None,
        property_ids=None,
        hotspot_ids=None,
        allow_larger_run=False,
        modes="walking,cycling",
        dry_run=False,
    )
    with pytest.raises(KeyboardInterrupt):
        cli._run(run_args, resume=False)
    run_id = next(value.run_root.iterdir()).name
    assert AccessibilityRunStore.open(value.run_root, run_id).manifest()["status"] == "interrupted"

    resumed_provider = FixtureAccessibilityRoutingProvider(
        GRAPH,
        routes={TravelMode.CYCLING: worker_route(TravelMode.CYCLING)},
    )
    monkeypatch.setattr(cli, "_provider", lambda *_: resumed_provider)
    resume_args = argparse.Namespace(
        config=str(tmp_path / "config.toml"),
        run_id=run_id,
        modes=None,
        dry_run=False,
    )
    manifest = cli._run(resume_args, resume=True)
    assert manifest["status"] in {"completed", "completed_with_warnings"}
    route_calls = [call for call in resumed_provider.calls if call[0] == "route"]
    assert len(route_calls) == 1
    assert route_calls[0][1].mode is TravelMode.CYCLING
