from __future__ import annotations

import json

import pytest

from pipeline.reference_data.london.matching import resolve_properties

pytestmark = pytest.mark.postgres


def _run(connection, dataset: str) -> int:
    return connection.execute(
        """insert into reference_data.dataset_runs (dataset_name,source_url,source_dataset_id,downloaded_at,source_schema,source_schema_sha256,content_sha256,feature_count,crs_srid,importer_version,validation_status,import_status,is_current)
        values (%s,'https://city.invalid','fixture',now(),'{}','aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',repeat(%s,64),1,26917,'test','passed','promoted',true) returning id""",
        (dataset, "a"),
    ).fetchone()[0]


def test_reference_geometry_and_conservative_shadow_resolution(postgres_database, postgres_target) -> None:
    connection = postgres_database
    address_run, building_run, parcel_run = (_run(connection, name) for name in ("municipal_addresses", "building_footprints", "parcels"))
    address_id = connection.execute("""insert into reference_data.municipal_addresses (dataset_run_id,source_object_id,full_address,normalized_address,normalized_civic_address,normalized_unit,source_attributes,geometry)
      values (%s,'1','1107 Sunset Street Unit 4','1107 SUNSET ST UNIT 4','1107 SUNSET ST','4','{}',st_setsrid(st_makepoint(500000,4800000),26917)) returning id""", (address_run,)).fetchone()[0]
    building_id = connection.execute("""insert into reference_data.building_footprints (dataset_run_id,source_object_id,source_attributes,geometry)
      values (%s,'1','{}',st_geomfromtext('POLYGON((499990 4799990,500010 4799990,500010 4800010,499990 4800010,499990 4799990))',26917)) returning id""", (building_run,)).fetchone()[0]
    parcel_id = connection.execute("""insert into reference_data.parcels (dataset_run_id,source_object_id,source_attributes,geometry)
      values (%s,'1','{}',st_geomfromtext('POLYGON((499980 4799980,500020 4799980,500020 4800020,499980 4800020,499980 4799980))',26917)) returning id""", (parcel_run,)).fetchone()[0]
    property_id = connection.execute("insert into public.housing_properties (normalized_address,display_address,address_complete) values ('1107 Sunset Street Unit 4','1107 Sunset Street Unit 4',true) returning id").fetchone()[0]
    result = resolve_properties(postgres_target.url)
    assert result["summary"]["address:EXACT_UNIT_MATCH"] == 1
    match = connection.execute("select municipal_address_id,building_footprint_id,parcel_id,address_match_method,building_match_method,parcel_match_method,review_required from reference_data.property_reference_matches where property_id=%s", (property_id,)).fetchone()
    assert match == (address_id, building_id, parcel_id, "EXACT_UNIT_MATCH", "EXACT_CONTAINMENT", "ADDRESS_CONTAINMENT", False)


def test_reference_runs_retain_history_on_new_current_snapshot(postgres_database) -> None:
    connection = postgres_database
    first = _run(connection, "municipal_addresses")
    connection.execute("update reference_data.dataset_runs set is_current=false where id=%s", (first,))
    second = connection.execute("""insert into reference_data.dataset_runs (dataset_name,source_url,source_dataset_id,downloaded_at,source_schema,source_schema_sha256,content_sha256,feature_count,crs_srid,importer_version,validation_status,import_status,is_current)
       values ('municipal_addresses','https://city.invalid','fixture',now(),'{}',repeat('a',64),repeat('b',64),1,26917,'test','passed','promoted',true) returning id""").fetchone()[0]
    assert connection.execute("select count(*) from reference_data.dataset_runs where dataset_name='municipal_addresses'").fetchone()[0] == 2
    assert connection.execute("select id from reference_data.dataset_runs where dataset_name='municipal_addresses' and is_current").fetchone()[0] == second
