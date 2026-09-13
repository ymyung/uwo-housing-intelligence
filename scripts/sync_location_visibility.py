"""Persist a complete reviewed location-visibility artifact."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path

from pipeline.location_visibility import (
    load_location_visibility,
    sync_location_visibility,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--database-url-env", default="DATABASE_URL")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    database_url = os.getenv(args.database_url_env, "").strip()
    if not database_url:
        raise SystemExit(f"{args.database_url_env} is required")
    rows = load_location_visibility(args.input)
    result = sync_location_visibility(database_url, rows)
    print(json.dumps(asdict(result), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
