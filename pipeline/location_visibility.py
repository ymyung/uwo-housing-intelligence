"""Validate and persist reviewed property-level location visibility decisions."""

from __future__ import annotations

import csv
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any


LOCATION_STATUSES = {"available", "limited", "unavailable"}


@dataclass(frozen=True)
class LocationVisibilityRow:
    property_id: int
    policy_version: str
    location_status: str
    map_visible: bool
    route_available: bool
    reason_codes: tuple[str, ...]
    source_run_id: str
    source_fingerprint: str


@dataclass(frozen=True)
class LocationVisibilitySyncResult:
    input_rows: int
    inserted_rows: int
    superseded_rows: int
    unchanged_rows: int


def _boolean(value: str, *, field: str, row_number: int) -> bool:
    text = value.strip().casefold()
    if text == "true":
        return True
    if text == "false":
        return False
    raise ValueError(f"row {row_number}: {field} must be true or false")


def load_location_visibility(path: Path) -> list[LocationVisibilityRow]:
    """Load a complete reviewed CSV and reject ambiguous or inconsistent rows."""

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        source_rows = list(csv.DictReader(handle))
    if not source_rows:
        raise ValueError("location visibility CSV contains no rows")

    rows: list[LocationVisibilityRow] = []
    seen: set[int] = set()
    for row_number, source in enumerate(source_rows, start=2):
        try:
            property_id = int(source.get("property_id") or "")
        except ValueError as exc:
            raise ValueError(f"row {row_number}: property_id must be an integer") from exc
        if property_id <= 0 or property_id in seen:
            problem = "must be positive" if property_id <= 0 else "is duplicated"
            raise ValueError(f"row {row_number}: property_id {problem}")
        seen.add(property_id)

        status = str(source.get("location_status") or "").strip()
        map_visible = _boolean(
            str(source.get("map_visible") or ""),
            field="map_visible",
            row_number=row_number,
        )
        route_available = _boolean(
            str(source.get("route_available") or ""),
            field="route_available",
            row_number=row_number,
        )
        expected = {
            "available": (True, True),
            "limited": (True, False),
            "unavailable": (False, False),
        }
        if status not in LOCATION_STATUSES:
            raise ValueError(f"row {row_number}: invalid location_status")
        if (map_visible, route_available) != expected[status]:
            raise ValueError(
                f"row {row_number}: visibility flags do not match {status} status"
            )

        try:
            raw_reasons = json.loads(source.get("reason_codes") or "[]")
        except json.JSONDecodeError as exc:
            raise ValueError(f"row {row_number}: reason_codes must be JSON") from exc
        if not isinstance(raw_reasons, list) or any(
            not isinstance(reason, str) or not reason.strip()
            for reason in raw_reasons
        ):
            raise ValueError(
                f"row {row_number}: reason_codes must be a JSON string array"
            )

        policy_version = str(source.get("policy_version") or "").strip()
        source_run_id = str(source.get("source_run_id") or "").strip()
        source_fingerprint = str(source.get("source_fingerprint") or "").strip()
        if not policy_version or not source_run_id:
            raise ValueError(
                f"row {row_number}: policy_version and source_run_id are required"
            )
        if len(source_fingerprint) != 64 or any(
            character not in "0123456789abcdef" for character in source_fingerprint
        ):
            raise ValueError(f"row {row_number}: source_fingerprint must be SHA-256")

        rows.append(
            LocationVisibilityRow(
                property_id=property_id,
                policy_version=policy_version,
                location_status=status,
                map_visible=map_visible,
                route_available=route_available,
                reason_codes=tuple(sorted(set(raw_reasons))),
                source_run_id=source_run_id,
                source_fingerprint=source_fingerprint,
            )
        )

    identities = {
        (row.policy_version, row.source_run_id, row.source_fingerprint)
        for row in rows
    }
    if len(identities) != 1:
        raise ValueError("all visibility rows must share one policy and source identity")
    return rows


def sync_location_visibility(
    database_url: str, rows: list[LocationVisibilityRow]
) -> LocationVisibilitySyncResult:
    """Transactionally replace changed current decisions while retaining history."""

    if not rows:
        raise ValueError("at least one location visibility row is required")
    try:
        import psycopg
        from psycopg.rows import dict_row
        from psycopg.types.json import Jsonb
    except ImportError as exc:  # pragma: no cover - deployment dependency guard
        raise RuntimeError("psycopg is required to sync location visibility") from exc

    inserted = 0
    superseded = 0
    unchanged = 0
    requested_ids = {row.property_id for row in rows}
    with psycopg.connect(database_url, row_factory=dict_row) as connection:
        connection.execute(
            "select pg_advisory_xact_lock(hashtext('uwo-location-visibility-sync'))"
        )
        active_ids = {
            int(row["property_id"])
            for row in connection.execute(
                "select distinct property_id from public.ranked_housing_listings"
            )
        }
        if requested_ids != active_ids:
            missing = len(active_ids - requested_ids)
            unexpected = len(requested_ids - active_ids)
            raise ValueError(
                "visibility input must cover the complete active property set "
                f"(missing={missing}, unexpected={unexpected})"
            )

        current = {
            int(row["property_id"]): dict(row)
            for row in connection.execute(
                """
                select id, property_id, policy_version, location_status,
                       map_visible, route_available, reason_codes,
                       source_run_id, source_fingerprint
                from public.housing_property_location_visibility
                where is_current
                for update
                """
            )
        }
        for row in rows:
            previous = current.get(row.property_id)
            same = previous is not None and (
                str(previous["policy_version"]) == row.policy_version
                and str(previous["location_status"]) == row.location_status
                and bool(previous["map_visible"]) is row.map_visible
                and bool(previous["route_available"]) is row.route_available
                and tuple(sorted(previous["reason_codes"] or [])) == row.reason_codes
                and str(previous["source_run_id"]) == row.source_run_id
                and str(previous["source_fingerprint"]) == row.source_fingerprint
            )
            if same:
                unchanged += 1
                continue
            if previous is not None:
                connection.execute(
                    """
                    update public.housing_property_location_visibility
                    set is_current = false, superseded_at = now()
                    where id = %s
                    """,
                    (previous["id"],),
                )
                superseded += 1
            connection.execute(
                """
                insert into public.housing_property_location_visibility (
                    property_id, policy_version, location_status, map_visible,
                    route_available, reason_codes, source_run_id,
                    source_fingerprint
                ) values (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    row.property_id,
                    row.policy_version,
                    row.location_status,
                    row.map_visible,
                    row.route_available,
                    Jsonb(list(row.reason_codes)),
                    row.source_run_id,
                    row.source_fingerprint,
                ),
            )
            inserted += 1

    return LocationVisibilitySyncResult(
        input_rows=len(rows),
        inserted_rows=inserted,
        superseded_rows=superseded,
        unchanged_rows=unchanged,
    )
