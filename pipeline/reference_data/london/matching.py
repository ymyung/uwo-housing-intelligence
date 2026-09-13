"""Conservative shadow resolver; it never modifies housing property coordinates."""
from __future__ import annotations

import uuid
from collections import Counter
from typing import Any

from .normalization import normalize_address


def resolve_properties(database_url: str) -> dict[str, Any]:
    import psycopg
    resolution_id = uuid.uuid4()
    with psycopg.connect(database_url) as connection, connection.transaction():
        runs = dict(connection.execute("select dataset_name,id from reference_data.dataset_runs where is_current").fetchall())
        if "municipal_addresses" not in runs:
            raise RuntimeError("A current municipal_addresses reference snapshot is required")
        properties = connection.execute("select id, normalized_address, display_address, unit_identifier, latitude, longitude from public.housing_properties order by id").fetchall()
        connection.execute("insert into reference_data.property_resolution_runs (id,address_dataset_run_id,building_dataset_run_id,parcel_dataset_run_id,property_count,status) values (%s,%s,%s,%s,%s,'running')", (resolution_id, runs["municipal_addresses"], runs.get("building_footprints"), runs.get("parcels"), len(properties)))
        connection.execute("update reference_data.property_reference_matches set is_current=false where is_current")
        summary: Counter[str] = Counter()
        for property_id, normalized_address, display_address, unit_identifier, latitude, longitude in properties:
            candidate = normalize_address(normalized_address or display_address)
            choices = [] if not candidate.civic_address else connection.execute(
                "select id, normalized_unit, geometry from reference_data.municipal_addresses where dataset_run_id=%s and normalized_civic_address=%s order by id", (runs["municipal_addresses"], candidate.civic_address)
            ).fetchall()
            address_id = None; reasons: list[str] = []; review = False
            if candidate.unit:
                exact = [choice for choice in choices if choice[1] == candidate.unit]
                if len(exact) == 1: address_id, method, confidence = exact[0][0], "EXACT_UNIT_MATCH", 1.0
                elif len(exact) > 1: method, confidence, review, reasons = "AMBIGUOUS", None, True, ["multiple_official_unit_matches"]
                else: method, confidence, review, reasons = "NO_MATCH", None, True, ["official_unit_not_found"]
            elif len(choices) == 1: address_id, method, confidence = choices[0][0], "EXACT_CIVIC_MATCH", 0.98
            elif len(choices) > 1: method, confidence, review, reasons = "AMBIGUOUS", None, True, ["multiple_official_civic_matches"]
            else: method, confidence, review, reasons = "NO_MATCH", None, True, ["official_address_not_found"]
            building_id = parcel_id = None
            building_method, parcel_method = "NO_BUILDING", "NO_PARCEL"
            if address_id and runs.get("building_footprints"):
                found = connection.execute("select id, geometry from reference_data.building_footprints where dataset_run_id=%s and st_covers(geometry,(select geometry from reference_data.municipal_addresses where id=%s))", (runs["building_footprints"], address_id)).fetchall()
                if len(found) == 1: building_id, building_method = found[0][0], "EXACT_CONTAINMENT"
                elif len(found) > 1: building_method, review = "AMBIGUOUS", True; reasons.append("multiple_building_containments")
            if address_id and runs.get("parcels"):
                found = connection.execute("select id from reference_data.parcels where dataset_run_id=%s and st_covers(geometry,(select geometry from reference_data.municipal_addresses where id=%s))", (runs["parcels"], address_id)).fetchall()
                if len(found) == 1: parcel_id, parcel_method = found[0][0], "ADDRESS_CONTAINMENT"
                elif len(found) > 1: parcel_method, review = "AMBIGUOUS", True; reasons.append("multiple_parcel_containments")
            connection.execute("""insert into reference_data.property_reference_matches
              (resolution_run_id,property_id,municipal_address_id,building_footprint_id,parcel_id,address_match_method,address_match_confidence,building_match_method,building_match_confidence,parcel_match_method,parcel_match_confidence,review_required,reason_codes,is_current)
              values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,true)""", (resolution_id, property_id, address_id, building_id, parcel_id, method, confidence, building_method, 1.0 if building_id else None, parcel_method, 1.0 if parcel_id else None, review, __import__('json').dumps(reasons)))
            summary[f"address:{method}"] += 1; summary[f"building:{building_method}"] += 1; summary[f"parcel:{parcel_method}"] += 1
        connection.execute("update reference_data.property_resolution_runs set status='completed',completed_at=now(),summary=%s where id=%s", (__import__('json').dumps(dict(summary)), resolution_id))
    return {"resolution_run_id": str(resolution_id), "summary": dict(summary)}
