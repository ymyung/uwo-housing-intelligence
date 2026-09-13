"""Download, validate, import, and shadow-match City of London reference data."""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipeline.reference_data.london.datasets import load_settings
from pipeline.reference_data.london.importer import (
    database_url_from_environment, download_snapshot, import_snapshot, read_snapshot,
)
from pipeline.reference_data.london.matching import resolve_properties
from pipeline.reference_data.london.validation import validate_snapshot


def _write_audits(database_url: str, settings) -> dict[str, object]:
    import psycopg
    settings.validation_root.mkdir(parents=True, exist_ok=True)
    with psycopg.connect(database_url) as connection:
        rows = connection.execute("""select m.property_id,m.address_match_method,m.building_match_method,m.parcel_match_method,m.review_required,
          case when p.latitude is not null and a.id is not null then st_distance(st_setsrid(st_makepoint(p.longitude,p.latitude),4326)::geography,st_transform(a.geometry,4326)::geography) end as distance_meters
          from reference_data.property_reference_matches m join public.housing_properties p on p.id=m.property_id
          left join reference_data.municipal_addresses a on a.id=m.municipal_address_id where m.is_current order by m.property_id""").fetchall()
        counts = connection.execute("select dataset_name,feature_count,invalid_feature_count,content_sha256 from reference_data.dataset_runs where is_current order by dataset_name").fetchall()
    headers = ["property_id", "address_match_method", "building_match_method", "parcel_match_method", "review_required", "distance_meters"]
    with (settings.validation_root / "city-address-match-audit.csv").open("w", newline="", encoding="utf-8") as output:
        writer = csv.writer(output); writer.writerow(headers); writer.writerows(rows)
    for filename, indexes in (("city-geocode-comparison.csv", [0, 1, 5]), ("building-resolution-audit.csv", [0, 2, 4]), ("parcel-resolution-audit.csv", [0, 3, 4])):
        with (settings.validation_root / filename).open("w", newline="", encoding="utf-8") as output:
            writer = csv.writer(output); writer.writerow([headers[index] for index in indexes]); writer.writerows([[row[index] for index in indexes] for row in rows])
    distances = sorted(row[5] for row in rows if row[5] is not None)
    def quantile(fraction: float):
        return None if not distances else distances[round((len(distances)-1)*fraction)]
    summary = {"datasets": [dict(zip(("dataset_name","feature_count","invalid_feature_count","content_sha256"), row)) for row in counts], "property_matches": len(rows), "review_required": sum(bool(row[4]) for row in rows), "distance_meters": {name: quantile(fraction) for name, fraction in (("min",0),("p25",.25),("median",.5),("p75",.75),("p90",.9),("p95",.95),("max",1))}}
    (settings.validation_root / "reference-data-summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("download", "validate", "import", "sync", "status", "match", "audit"))
    parser.add_argument("--dataset", choices=("municipal_addresses", "building_footprints", "parcels"))
    parser.add_argument("--snapshot", type=Path)
    args = parser.parse_args(); settings = load_settings()
    selected = [settings.datasets[args.dataset]] if args.dataset else list(settings.datasets.values())
    if args.command == "download":
        print(json.dumps({spec.name: str(download_snapshot(settings, spec)) for spec in selected}, indent=2)); return
    if args.command == "validate":
        if not args.snapshot or not args.dataset: parser.error("validate requires --dataset and --snapshot")
        metadata, features = read_snapshot(args.snapshot); print(validate_snapshot(selected[0], metadata, features)); return
    url = database_url_from_environment()
    if args.command in ("import", "sync"):
        results = []
        for spec in selected:
            snapshot = args.snapshot if args.snapshot else download_snapshot(settings, spec)
            results.append(import_snapshot(url, settings, spec, snapshot))
        print(json.dumps(results, indent=2, default=str))
    if args.command in ("match", "sync"):
        print(json.dumps(resolve_properties(url), indent=2))
    if args.command in ("audit", "sync", "status"):
        print(json.dumps(_write_audits(url, settings), indent=2))


if __name__ == "__main__":
    main()
