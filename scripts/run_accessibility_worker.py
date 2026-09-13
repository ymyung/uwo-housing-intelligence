"""CLI for the bounded self-hosted accessibility routing worker."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from backend.accessibility_inputs import (
    AccessibilityInputError,
    AccessibilityWorkerConfig,
    load_reviewed_properties,
    load_routing_bundle,
    load_verified_hotspots,
    load_worker_config,
    parse_modes,
    stable_fingerprint,
)
from backend.accessibility_repository import (
    InMemoryAccessibilityRepository,
    PostgresAccessibilityRepository,
)
from backend.accessibility_runs import AccessibilityRunStore
from backend.accessibility_worker import AccessibilityWorker, build_work_units
from backend.routing_provider import OpenTripPlannerProvider, RoutingProviderError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "accessibility-worker.toml"
LOGGER = logging.getLogger(__name__)


def _ids(value: str | None, *, integers: bool) -> set[Any] | None:
    if not value:
        return None
    items = {item.strip() for item in value.split(",") if item.strip()}
    if not items:
        return None
    if integers:
        try:
            return {int(item) for item in items}
        except ValueError as exc:
            raise AccessibilityInputError("Property IDs must be integers") from exc
    return items


def _repository(config: AccessibilityWorkerConfig):
    if not config.persist:
        return InMemoryAccessibilityRepository()
    database_url = os.getenv(config.database_url_env, "").strip()
    if not database_url:
        raise AccessibilityInputError(
            f"{config.database_url_env} is required when persistence is enabled"
        )
    return PostgresAccessibilityRepository(database_url)


def _provider(config: AccessibilityWorkerConfig, bundle):
    return OpenTripPlannerProvider(
        base_url=config.otp_base_url,
        router_id=config.otp_router_id,
        timeout_seconds=config.otp_request_timeout_seconds,
        metadata=bundle.metadata,
    )


def _selection(
    args: argparse.Namespace,
    config: AccessibilityWorkerConfig,
    *,
    properties_path: Path | None = None,
    hotspots_path: Path | None = None,
):
    properties_path = properties_path or (
        Path(args.properties).resolve()
        if getattr(args, "properties", None)
        else config.properties_path
    )
    properties = load_reviewed_properties(
        properties_path,
        bounds=config.bounds,
        reviewed_only=config.reviewed_only,
        selected_ids=_ids(getattr(args, "property_ids", None), integers=True),
        limit=config.property_limit,
        allow_larger_run=getattr(args, "allow_larger_run", False),
    )
    hotspots = load_verified_hotspots(
        hotspots_path or config.hotspot_config_path,
        bounds=config.bounds,
        selected_ids=_ids(getattr(args, "hotspot_ids", None), integers=False),
        limit=config.hotspot_limit,
        allow_larger_run=getattr(args, "allow_larger_run", False),
    )
    return properties_path, properties, hotspots


def _worker(config, repository, provider, bundle) -> AccessibilityWorker:
    reference = datetime.combine(
        config.reference_service_week,
        datetime.min.time(),
        tzinfo=ZoneInfo("America/Toronto"),
    )
    return AccessibilityWorker(
        repository,
        provider,
        provider_profile=config.provider_profile,
        graph_metadata=bundle.metadata,
        reference_service_week=reference,
        minimum_transit_samples=config.minimum_transit_samples,
        max_retries=config.max_retries,
        retry_delay_seconds=config.retry_delay_seconds,
        persist=config.persist,
    )


def _run(args: argparse.Namespace, *, resume: bool) -> dict[str, Any]:
    config_path = Path(args.config).resolve()
    config = load_worker_config(config_path)
    bundle = load_routing_bundle(config)
    store = None
    if resume:
        store = AccessibilityRunStore.open(config.run_root, args.run_id)
        existing_manifest = store.manifest()
        modes = parse_modes(
            args.modes or ",".join(existing_manifest.get("requested_modes") or [])
        )
        properties_path, properties, hotspots = _selection(
            args,
            config,
            properties_path=store.paths.selected_properties,
            hotspots_path=store.paths.selected_hotspots,
        )
    else:
        existing_manifest = None
        modes = parse_modes(args.modes)
        properties_path, properties, hotspots = _selection(args, config)
    selection_fingerprint = stable_fingerprint(
        {
            "properties": [value.fingerprint for value in properties],
            "hotspots": [value.fingerprint for value in hotspots],
        }
    )
    input_fingerprints = {
        **bundle.fingerprints,
        "selection_sha256": selection_fingerprint,
    }
    units = build_work_units(properties, hotspots, modes)
    repository = _repository(config)
    provider = _provider(config, bundle)
    worker = _worker(config, repository, provider, bundle)
    if resume and existing_manifest:
        prior_outcomes = store.outcomes()
        last_outcome_metrics = (
            prior_outcomes[-1].get("metrics_after_unit") if prior_outcomes else None
        )
        prior_metrics = (
            last_outcome_metrics
            if isinstance(last_outcome_metrics, dict)
            else existing_manifest.get("metrics") or {}
        )
        for name in worker.metrics.__dict__:
            if name in prior_metrics:
                setattr(worker.metrics, name, int(prior_metrics[name]))
    if args.dry_run:
        return {
            "status": "dry_run",
            "selected_property_count": len(properties),
            "selected_hotspot_count": len(hotspots),
            "work_unit_count": len(units),
            "requested_modes": [mode.value for mode in modes],
            "routing_provider_calls_made": 0,
            "database_writes_made": 0,
            "estimate": worker.estimate(units),
            "input_fingerprints": input_fingerprints,
        }
    if resume:
        if existing_manifest.get("input_fingerprints") != input_fingerprints:
            raise AccessibilityInputError("Routing inputs changed since the run started")
        expected_modes = existing_manifest.get("requested_modes")
        if expected_modes != [mode.value for mode in modes]:
            raise AccessibilityInputError("Requested modes differ from the original run")
    else:
        store = AccessibilityRunStore.create(
            config.run_root,
            properties=properties,
            hotspots=hotspots,
            modes=[mode.value for mode in modes],
            provider=provider.provider_name,
            provider_profile=config.provider_profile,
            graph_metadata=bundle.metadata.to_dict(),
            input_fingerprints=input_fingerprints,
            config_path=config_path,
            properties_path=properties_path,
        )
    provider.preflight(bundle.metadata)
    completed = {row["unit_key"] for row in store.outcomes()}
    try:
        for unit in units:
            if unit.key in completed:
                continue
            outcome = worker.process(unit, worker_run_id=store.run_id)
            store.record(outcome, worker.metrics.to_dict())
    except BaseException:
        store.finalize(interrupted=True)
        raise
    return store.finalize()


def _preflight(args: argparse.Namespace) -> dict[str, Any]:
    config = load_worker_config(Path(args.config).resolve())
    bundle = load_routing_bundle(config)
    _, properties, hotspots = _selection(args, config)
    provider = _provider(config, bundle)
    metadata = provider.preflight(bundle.metadata)
    database_status = "disabled"
    if config.persist:
        repository = _repository(config)
        with repository.connect() as connection:
            connection.execute("select 1").fetchone()
        database_status = "reachable"
    return {
        "status": "ok",
        "routing_endpoint": "reachable",
        "database": database_status,
        "selected_property_count": len(properties),
        "selected_hotspot_count": len(hotspots),
        "routing_metadata": metadata.to_dict(),
        "input_fingerprints": bundle.fingerprints,
    }


def _manifest_command(args: argparse.Namespace, *, summary: bool) -> dict[str, Any]:
    config = load_worker_config(Path(args.config).resolve())
    manifest = AccessibilityRunStore.open(config.run_root, args.run_id).manifest()
    if not summary:
        return manifest
    return {
        key: manifest.get(key)
        for key in (
            "run_id",
            "status",
            "started_at",
            "completed_at",
            "selected_property_count",
            "selected_hotspot_count",
            "requested_modes",
            "metrics",
            "success_count",
            "failure_count",
            "quality_warnings",
        )
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verbose", action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_config(command: argparse.ArgumentParser) -> None:
        command.add_argument("--config", default=str(DEFAULT_CONFIG))

    def add_selection(command: argparse.ArgumentParser) -> None:
        command.add_argument("--properties")
        command.add_argument("--property-ids")
        command.add_argument("--hotspot-ids")
        command.add_argument("--allow-larger-run", action="store_true")

    preflight = subparsers.add_parser("preflight")
    add_config(preflight)
    add_selection(preflight)

    run = subparsers.add_parser("run")
    add_config(run)
    add_selection(run)
    run.add_argument("--modes", default="walking,cycling,transit")
    run.add_argument("--dry-run", action="store_true")

    resume = subparsers.add_parser("resume")
    add_config(resume)
    resume.add_argument("--run-id", required=True)
    resume.add_argument("--modes")
    resume.add_argument("--dry-run", action="store_true")

    for name in ("status", "summary"):
        command = subparsers.add_parser(name)
        add_config(command)
        command.add_argument("--run-id", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    try:
        if args.command == "preflight":
            result = _preflight(args)
        elif args.command == "run":
            result = _run(args, resume=False)
        elif args.command == "resume":
            result = _run(args, resume=True)
        else:
            result = _manifest_command(args, summary=args.command == "summary")
    except (AccessibilityInputError, RoutingProviderError, ValueError, RuntimeError) as exc:
        LOGGER.error("Accessibility worker failed: %s", exc)
        return 2
    sys.stdout.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
