"""Dry-run, compute, persist, and summarize deterministic Ranking v1 scores."""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Iterable

from backend.ranking_repository import PostgresRankingRepository
from backend.ranking_v1 import (
    MarketBaseline,
    RankingConfig,
    RankingResult,
    build_market_baselines,
    load_ranking_config,
    ranking_input_fingerprint,
    ranking_output_fingerprint,
    score_with_baselines,
)
from pipeline.run_context import atomic_write_json, generate_run_id, get_git_metadata


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config" / "ranking-v1.toml"
DEFAULT_DRY_RUN = ROOT / "data" / "ranking-validation" / "ranking-dry-run.json"
LOGGER = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_text(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _database_url(config: RankingConfig) -> str:
    value = os.getenv(config.database_url_env, "").strip()
    if not value:
        raise RuntimeError(
            f"{config.database_url_env} is required; initialize the documented local environment"
        )
    return value


def _quantile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    offset = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * offset


def _distribution(results: Iterable[RankingResult], field: str) -> dict[str, Any]:
    values = [
        float(value)
        for result in results
        if (value := getattr(result, field)) is not None
    ]
    return {
        "count": len(values),
        "min": round(min(values), 2) if values else None,
        "p10": round(_quantile(values, 0.10), 2) if values else None,
        "p25": round(_quantile(values, 0.25), 2) if values else None,
        "median": round(_quantile(values, 0.50), 2) if values else None,
        "p75": round(_quantile(values, 0.75), 2) if values else None,
        "p90": round(_quantile(values, 0.90), 2) if values else None,
        "max": round(max(values), 2) if values else None,
        "mean": round(sum(values) / len(values), 2) if values else None,
    }


def _summarize(
    results: list[RankingResult],
    baselines: dict[int, MarketBaseline],
    config: RankingConfig,
) -> dict[str, Any]:
    statuses = {
        status: sum(result.ranking_status == status for result in results)
        for status in ("ranked", "partial", "excluded")
    }
    missing_price = sum(
        "missing_monthly_price" in result.explanation["eligibility_reasons"]
        for result in results
    )
    fallback_usage: dict[str, int] = {}
    cohort_sizes: list[int] = []
    for result in results:
        if result.ranking_status != "ranked":
            continue
        baseline = baselines[result.listing_id]
        fallback_usage[baseline.level] = fallback_usage.get(baseline.level, 0) + 1
        cohort_sizes.append(baseline.count)
    return {
        "ranking_version": config.version,
        "config_fingerprint": config.fingerprint,
        "active_listing_count": len(results),
        "trusted_accessibility_listing_count": statuses["ranked"] + statuses["partial"],
        "ranking_status_counts": statuses,
        "missing_monthly_price_count": missing_price,
        "market_population_count": len(baselines),
        "comparable_fallback_usage": fallback_usage,
        "smallest_selected_comparable_count": min(cohort_sizes) if cohort_sizes else None,
        "score_distributions": {
            "overall_score": _distribution(results, "overall_score"),
            "value_score": _distribution(results, "value_score"),
            "campus_access_score": _distribution(results, "campus_access_score"),
            "transit_score": _distribution(results, "transit_score"),
            "amenity_score": _distribution(results, "amenity_score"),
            "data_quality_score": _distribution(results, "data_quality_score"),
        },
        "amenity_status": "not_implemented",
        "data_quality_component_status": "not_scored",
    }


def _compute(
    repository: PostgresRankingRepository, config: RankingConfig
) -> tuple[list[RankingResult], dict[int, MarketBaseline], dict[str, float]]:
    total_started = time.perf_counter()
    started = time.perf_counter()
    features = repository.load_listing_features(config)
    feature_seconds = time.perf_counter() - started
    started = time.perf_counter()
    baselines = build_market_baselines(features, config)
    baseline_seconds = time.perf_counter() - started
    started = time.perf_counter()
    results = score_with_baselines(features, baselines, config)
    scoring_seconds = time.perf_counter() - started
    return results, baselines, {
        "feature_extraction_seconds": round(feature_seconds, 6),
        "market_baseline_seconds": round(baseline_seconds, 6),
        "scoring_seconds": round(scoring_seconds, 6),
        "pre_persistence_seconds": round(time.perf_counter() - total_started, 6),
    }


def _atomic_csv(path: Path, rows: list[RankingResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with temporary.open("w", encoding="utf-8", newline="") as output:
            fieldnames = [
                "listing_id",
                "source_listing_id",
                "property_id",
                "ranking_status",
                "overall_score",
                "value_score",
                "campus_access_score",
                "transit_score",
                "amenity_score",
                "data_quality_score",
                "input_fingerprint",
                "explanation",
            ]
            writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
            writer.writeheader()
            for result in rows:
                writer.writerow(
                    {
                        **{
                            field: getattr(result, field)
                            for field in fieldnames
                            if field != "explanation"
                        },
                        "explanation": json.dumps(
                            result.explanation,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    }
                )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def dry_run(config_path: Path, output: Path) -> dict[str, Any]:
    config = load_ranking_config(config_path)
    repository = PostgresRankingRepository(_database_url(config))
    results, baselines, timings = _compute(repository, config)
    current = repository.current_input_fingerprints(config.version)
    expected_inserts = sum(
        current.get(result.listing_id) != result.input_fingerprint for result in results
    )
    summary = {
        "schema_version": 1,
        "command": "dry-run",
        "database_writes": 0,
        "expected_score_row_inserts": expected_inserts,
        "expected_score_rows_superseded": sum(
            result.listing_id in current
            and current[result.listing_id] != result.input_fingerprint
            for result in results
        ),
        "input_fingerprint": ranking_input_fingerprint(results),
        "output_fingerprint": ranking_output_fingerprint(results),
        "timings": timings,
        **_summarize(results, baselines, config),
    }
    atomic_write_json(output, summary)
    return summary


def run(config_path: Path) -> dict[str, Any]:
    config = load_ranking_config(config_path)
    repository = PostgresRankingRepository(_database_url(config))
    if not repository.ranking_schema_present():
        raise RuntimeError("Ranking persistence migration is not applied")
    started_at = _utc_now()
    total_started = time.perf_counter()
    results, baselines, timings = _compute(repository, config)
    input_fingerprint = ranking_input_fingerprint(results)
    output_fingerprint = ranking_output_fingerprint(results)
    git_commit, git_dirty = get_git_metadata(ROOT)
    run_id = generate_run_id(git_commit=git_commit)
    ranking_run_id = repository.start_run(
        run_id=run_id,
        config=config,
        input_fingerprint=input_fingerprint,
        listing_count=len(results),
        started_at=started_at,
    )
    try:
        started = time.perf_counter()
        completed_at = _utc_now()
        summary = _summarize(results, baselines, config)
        writes = repository.complete_run(
            ranking_run_id=ranking_run_id,
            results=results,
            output_fingerprint=output_fingerprint,
            summary={**summary, "timings": timings},
            completed_at=completed_at,
        )
        timings["persistence_seconds"] = round(time.perf_counter() - started, 6)
        timings["total_seconds"] = round(time.perf_counter() - total_started, 6)
    except Exception as error:
        repository.fail_run(
            ranking_run_id,
            f"{type(error).__name__}: ranking persistence failed",
            _utc_now(),
        )
        raise
    manifest = {
        "schema_version": 1,
        "run_id": run_id,
        "ranking_run_id": ranking_run_id,
        "ranking_version": config.version,
        "status": "completed",
        "git_commit": git_commit,
        "git_dirty": git_dirty,
        "started_at": _utc_text(started_at),
        "completed_at": _utc_text(completed_at),
        "config_path": str(config_path.resolve()),
        "config_fingerprint": config.fingerprint,
        "input_fingerprint": input_fingerprint,
        "output_fingerprint": output_fingerprint,
        "score_rows_inserted": writes["inserted"],
        "score_rows_superseded": writes["superseded"],
        "database_writes": writes["inserted"] + writes["superseded"],
        "timings": timings,
        **summary,
    }
    run_root = config.run_root if config.run_root.is_absolute() else ROOT / config.run_root
    artifact_root = run_root / run_id
    artifact_root.mkdir(parents=True, exist_ok=False)
    atomic_write_json(artifact_root / "manifest.json", manifest)
    _atomic_csv(artifact_root / "ranking-results.csv", results)
    return manifest


def read_summary(config_path: Path, run_id: str) -> dict[str, Any]:
    config = load_ranking_config(config_path)
    run_root = config.run_root if config.run_root.is_absolute() else ROOT / config.run_root
    path = run_root / run_id / "manifest.json"
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict) or value.get("run_id") != run_id:
        raise ValueError(f"Invalid ranking run manifest: {run_id}")
    return value


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--verbose", action="store_true")
    commands = root.add_subparsers(dest="command", required=True)
    dry = commands.add_parser("dry-run")
    dry.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    dry.add_argument("--output", type=Path, default=DEFAULT_DRY_RUN)
    execute = commands.add_parser("run")
    execute.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    summary = commands.add_parser("summary")
    summary.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    summary.add_argument("--run-id", required=True)
    return root


def main() -> None:
    args = parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    if args.command == "dry-run":
        value = dry_run(args.config.resolve(), args.output.resolve())
    elif args.command == "run":
        value = run(args.config.resolve())
    else:
        value = read_summary(args.config.resolve(), args.run_id)
    print(json.dumps(value, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
