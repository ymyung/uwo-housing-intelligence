"""Generate the ignored, read-only coordinate-promotion-v1 migration plan."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipeline.reference_data.london.coordinate_promotion import (
    build_promotion_plan,
    load_promotion_config,
    write_promotion_artifacts,
)
from scripts.generate_coordinate_selection_shadow import (
    local_database_url_from_environment,
)


def default_output_dir(planned_at: datetime) -> Path:
    timestamp = planned_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return (
        PROJECT_ROOT
        / "data"
        / "london-reference-validation"
        / "coordinate-promotion-v1"
        / timestamp
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=PROJECT_ROOT / "config" / "coordinate-promotion-v1.toml",
    )
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()

    planned_at = datetime.now(timezone.utc)
    config = load_promotion_config(args.config)
    output_dir = args.output_dir or default_output_dir(planned_at)

    import psycopg

    with psycopg.connect(local_database_url_from_environment()) as connection:
        with connection.transaction():
            connection.execute("set transaction read only")
            plan = build_promotion_plan(
                connection,
                config,
                project_root=PROJECT_ROOT,
                planned_at=planned_at,
            )

    paths = write_promotion_artifacts(plan, output_dir)
    print(
        json.dumps(
            {
                "promotion_policy_version": config.version,
                "mode": "dry_run_only",
                "candidate_run_id": config.candidate_run_id,
                "fresh_eligible_cohort": len(plan.eligible),
                "stale_candidate_count": len(plan.stale),
                "dependency_impact": plan.dependency_impact,
                "movement_audit": plan.movement_audit,
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
