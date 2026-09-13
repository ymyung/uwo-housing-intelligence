"""Generate an ignored, read-only coordinate-selection-v1 candidate audit."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipeline.reference_data.london.coordinate_selection import (
    load_coordinate_selection_policy,
)
from pipeline.reference_data.london.coordinate_selection_shadow import (
    generate_shadow_candidate,
    policy_fingerprint,
    write_shadow_artifacts,
)


LOCAL_DATABASE_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def local_database_url_from_environment() -> str:
    """Return the explicitly configured loopback-only analysis database URL."""

    value = os.environ.get("ACCESSIBILITY_DATABASE_URL", "").strip()
    if not value:
        raise RuntimeError(
            "ACCESSIBILITY_DATABASE_URL is required for local shadow analysis"
        )
    parsed = urlparse(value)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise RuntimeError("ACCESSIBILITY_DATABASE_URL must be a PostgreSQL URL")
    if parsed.hostname not in LOCAL_DATABASE_HOSTS:
        raise RuntimeError(
            "coordinate shadow analysis is restricted to a local loopback database"
        )
    return value


def default_output_dir(evaluated_at: datetime) -> Path:
    timestamp = evaluated_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return (
        PROJECT_ROOT
        / "data"
        / "london-reference-validation"
        / "coordinate-selection-v1"
        / timestamp
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=PROJECT_ROOT / "config" / "coordinate-selection-v1.toml",
    )
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()

    evaluated_at = datetime.now(timezone.utc)
    policy = load_coordinate_selection_policy(args.config)
    output_dir = args.output_dir or default_output_dir(evaluated_at)

    import psycopg

    with psycopg.connect(local_database_url_from_environment()) as connection:
        with connection.transaction():
            connection.execute("set transaction read only")
            result = generate_shadow_candidate(
                connection,
                policy,
                evaluated_at=evaluated_at,
            )

    result.summary["policy_config_sha256"] = policy_fingerprint(args.config)
    paths = write_shadow_artifacts(result, output_dir)
    print(
        json.dumps(
            {
                "policy_version": policy.version,
                "mode": result.summary["mode"],
                "decision_counts": result.summary["decision_counts"],
                "runtime_seconds": result.runtime_seconds,
                "query_count": result.query_count,
                "output_dir": str(output_dir.resolve()),
                "artifacts": {
                    name: str(path.resolve()) for name, path in paths.items()
                },
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
