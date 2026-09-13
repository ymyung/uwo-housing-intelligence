"""Guarded local operator for coordinate-promotion-v1."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipeline.reference_data.london.coordinate_promotion import (
    BeforeStateSnapshot,
    build_promotion_plan,
    capture_before_state,
    execute_local_promotion,
    finalize_local_promotion,
    load_promotion_config,
    promotion_state_fingerprint,
    reevaluate_promoted_location_visibility,
    rollback_disposable_promotion,
    set_local_promotion_status,
    validate_post_cutover,
    write_before_state_snapshot,
)


DATABASE_ENV = "ACCESSIBILITY_DATABASE_URL"
MIGRATION = (
    PROJECT_ROOT
    / "supabase"
    / "migrations"
    / "20260825000100_create_coordinate_promotion_store.sql"
)


def _database_url() -> str:
    value = os.getenv(DATABASE_ENV, "").strip()
    if not value:
        raise RuntimeError(f"{DATABASE_ENV} is required")
    parsed = urlparse(value)
    if (
        parsed.scheme not in {"postgres", "postgresql"}
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.port != 55600
        or parsed.path != "/uwo_housing_dev"
    ):
        raise RuntimeError(
            "coordinate promotion requires loopback uwo_housing_dev on port 55600"
        )
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_snapshot(path: Path, expected_sha256: str) -> BeforeStateSnapshot:
    actual = _sha256(path)
    if actual != expected_sha256:
        raise RuntimeError("before-state artifact fingerprint mismatch")
    state = json.loads(path.read_text(encoding="utf-8-sig"))
    fingerprint = str(state.get("state_fingerprint") or "")
    if len(fingerprint) != 64:
        raise RuntimeError("before-state logical fingerprint is invalid")
    return BeforeStateSnapshot(
        state=state,
        state_fingerprint=fingerprint,
        artifact_fingerprint=actual,
    )


def _plan(connection):
    config = load_promotion_config()
    plan = build_promotion_plan(connection, config, project_root=PROJECT_ROOT)
    if len(plan.eligible) != 218 or plan.stale:
        raise RuntimeError("hard preflight cohort/freshness gate failed")
    if plan.movement_audit["buckets"][">100"] != 0:
        raise RuntimeError("hard preflight movement gate failed")
    return plan


def _read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("preflight")
    snapshot = commands.add_parser("snapshot")
    snapshot.add_argument("--output", type=Path, required=True)
    store = commands.add_parser("apply-store")
    store.add_argument("--backup-path", type=Path, required=True)
    store.add_argument("--backup-sha256", required=True)
    cutover = commands.add_parser("cutover")
    cutover.add_argument("--run-id", required=True)
    cutover.add_argument("--backup-path", type=Path, required=True)
    cutover.add_argument("--backup-sha256", required=True)
    cutover.add_argument("--snapshot", type=Path, required=True)
    cutover.add_argument("--snapshot-sha256", required=True)
    status = commands.add_parser("status")
    status.add_argument("--run-id", required=True)
    recomputing = commands.add_parser("mark-recomputing")
    recomputing.add_argument("--run-id", required=True)
    visibility = commands.add_parser("visibility")
    visibility.add_argument("--run-id", required=True)
    finalize = commands.add_parser("finalize")
    finalize.add_argument("--run-id", required=True)
    finalize.add_argument("--recomputation-summary", type=Path, required=True)
    finalize.add_argument("--validation-summary", type=Path, required=True)
    rollback = commands.add_parser("rollback")
    rollback.add_argument("--run-id", required=True)
    rollback.add_argument("--rollback-run-id", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    import psycopg

    with psycopg.connect(_database_url(), autocommit=True) as connection:
        if args.command == "preflight":
            plan = _plan(connection)
            result = {
                "status": "ready",
                "eligible": len(plan.eligible),
                "stale": len(plan.stale),
                "movement": plan.movement_audit,
                "dependencies": plan.dependency_impact,
            }
        elif args.command == "snapshot":
            plan = _plan(connection)
            snapshot = write_before_state_snapshot(
                capture_before_state(connection, plan), args.output.resolve()
            )
            result = {
                "path": str(args.output.resolve()),
                "state_fingerprint": snapshot.state_fingerprint,
                "artifact_sha256": snapshot.artifact_fingerprint,
                "properties": snapshot.state["property_count"],
                "latest_projections": snapshot.state["latest_projection_count"],
            }
        elif args.command == "apply-store":
            backup_path = args.backup_path.resolve()
            if not backup_path.is_file() or _sha256(backup_path) != args.backup_sha256:
                raise RuntimeError("verified pre-migration backup is required")
            if connection.execute(
                "select to_regclass('public.housing_coordinate_promotion_runs')"
            ).fetchone()[0] is not None:
                raise RuntimeError("coordinate promotion store already exists")
            with connection.transaction():
                connection.execute(MIGRATION.read_text(encoding="utf-8"))
            result = {"status": "applied", "migration": MIGRATION.name}
        elif args.command == "cutover":
            backup_path = args.backup_path.resolve()
            if not backup_path.is_file() or _sha256(backup_path) != args.backup_sha256:
                raise RuntimeError("backup fingerprint mismatch")
            snapshot = _load_snapshot(
                args.snapshot.resolve(), args.snapshot_sha256
            )
            plan = _plan(connection)
            writes = execute_local_promotion(
                connection,
                plan,
                migration_run_id=args.run_id,
                before_state_fingerprint=snapshot.state_fingerprint,
                backup_path=str(backup_path),
                backup_sha256=args.backup_sha256,
                project_root=PROJECT_ROOT,
            )
            validation = validate_post_cutover(
                connection,
                plan,
                snapshot,
                migration_run_id=args.run_id,
            )
            result = {"status": "cutover_completed", "writes": writes, "validation": validation}
        elif args.command == "mark-recomputing":
            set_local_promotion_status(connection, args.run_id, "recomputing")
            result = {"status": "recomputing", "run_id": args.run_id}
        elif args.command == "visibility":
            result = reevaluate_promoted_location_visibility(
                connection,
                load_promotion_config(),
                migration_run_id=args.run_id,
                project_root=PROJECT_ROOT,
            )
        elif args.command == "finalize":
            fingerprint = finalize_local_promotion(
                connection,
                args.run_id,
                recomputation_summary=_read_json(args.recomputation_summary),
                validation_summary=_read_json(args.validation_summary),
            )
            result = {"status": "completed", "final_state_fingerprint": fingerprint}
        elif args.command == "rollback":
            result = rollback_disposable_promotion(
                connection,
                promotion_run_id=args.run_id,
                rollback_run_id=args.rollback_run_id,
                allow_local_development=True,
            )
        else:
            row = connection.execute(
                """
                select run_id,status,eligible_property_count,stale_property_count,
                       before_state_fingerprint,backup_sha256,
                       final_state_fingerprint,started_at,completed_at,
                       summary,recomputation_summary,validation_summary
                from public.housing_coordinate_promotion_runs where run_id=%s
                """,
                (args.run_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("promotion run not found")
            result = {
                "run_id": row[0],
                "status": row[1],
                "eligible": row[2],
                "stale": row[3],
                "before_state_fingerprint": str(row[4]).strip(),
                "backup_sha256": str(row[5]).strip(),
                "final_state_fingerprint": (
                    str(row[6]).strip() if row[6] is not None else None
                ),
                "started_at": row[7].isoformat(),
                "completed_at": row[8].isoformat() if row[8] else None,
                "summary": row[9],
                "recomputation_summary": row[10],
                "validation_summary": row[11],
                "current_state_fingerprint": promotion_state_fingerprint(
                    connection, args.run_id
                ),
            }
    sys.stdout.write(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
