from __future__ import annotations

import csv
import io
import json
import zipfile
from datetime import date
from pathlib import Path

import pytest

from backend.gtfs_freshness import GtfsValidationError, inspect_gtfs_feed
from scripts.manage_gtfs import _safe_source_label, main


def csv_text(fields: list[str], rows: list[dict[str, object]]) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def gtfs_zip(
    path: Path, *, end: str = "20260621", calendar_dates: str = ""
) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name in ("agency.txt", "stops.txt", "routes.txt", "trips.txt", "stop_times.txt"):
            archive.writestr(name, "id\n1\n")
        archive.writestr(
            "feed_info.txt",
            csv_text(
                [
                    "feed_publisher_name",
                    "feed_publisher_url",
                    "feed_lang",
                    "feed_start_date",
                    "feed_end_date",
                    "feed_version",
                ],
                [
                    {
                        "feed_publisher_name": "London Transit",
                        "feed_publisher_url": "https://example.test",
                        "feed_lang": "en",
                        "feed_start_date": "20260601",
                        "feed_end_date": end,
                        "feed_version": "fixture-v1",
                    }
                ],
            ),
        )
        archive.writestr(
            "calendar.txt",
            csv_text(
                [
                    "service_id",
                    "monday",
                    "tuesday",
                    "wednesday",
                    "thursday",
                    "friday",
                    "saturday",
                    "sunday",
                    "start_date",
                    "end_date",
                ],
                [
                    {
                        "service_id": "daily",
                        "monday": 1,
                        "tuesday": 1,
                        "wednesday": 1,
                        "thursday": 1,
                        "friday": 1,
                        "saturday": 1,
                        "sunday": 1,
                        "start_date": "20260601",
                        "end_date": end,
                    }
                ],
            ),
        )
        archive.writestr(
            "calendar_dates.txt",
            "service_id,date,exception_type\n" + calendar_dates,
        )
    return path


def test_gtfs_service_range_reference_week_and_staleness(tmp_path: Path) -> None:
    path = gtfs_zip(tmp_path / "feed.zip")
    report = inspect_gtfs_feed(
        path,
        reference_week_start=date(2026, 6, 15),
        as_of_date=date(2026, 8, 9),
    )
    assert report.service_start_date == date(2026, 6, 1)
    assert report.service_end_date == date(2026, 6, 21)
    assert report.reference_week_supported is True
    assert report.expired is True
    assert report.graph_rebuild_recommended is True
    assert report.schedule_version.startswith("gtfs-sha256-")


def test_calendar_dates_can_extend_actual_coverage(tmp_path: Path) -> None:
    path = gtfs_zip(
        tmp_path / "feed.zip",
        end="20260620",
        calendar_dates="daily,20260621,1\n",
    )
    report = inspect_gtfs_feed(
        path,
        reference_week_start=date(2026, 6, 15),
        as_of_date=date(2026, 6, 21),
    )
    assert report.service_end_date == date(2026, 6, 21)
    assert report.reference_week_supported is True
    assert report.expired is False


def test_invalid_or_unsafe_gtfs_is_rejected(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.zip"
    invalid.write_text("not a zip", encoding="utf-8")
    with pytest.raises(GtfsValidationError, match="valid ZIP"):
        inspect_gtfs_feed(
            invalid,
            reference_week_start=date(2026, 6, 15),
        )


def test_stage_dry_run_validates_without_replacing_active_feed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = gtfs_zip(tmp_path / "candidate.zip")
    assert main(
        [
            "stage",
            "--source",
            str(source),
            "--config",
            "config/accessibility-worker.example.toml",
            "--staging-root",
            str(tmp_path / "staging"),
            "--dry-run",
            "--as-of",
            "2026-08-09",
        ]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["promotion_status"] == "not_promoted"
    assert payload["staged_directory"] is None
    assert not list((tmp_path / "staging").glob("gtfs-candidate-*"))


def test_source_labels_remove_url_query_credentials() -> None:
    label = _safe_source_label(
        "https://official.example/gtfs.zip?token=secret&signature=hidden"
    )
    assert label == "https://official.example/gtfs.zip"
    assert "secret" not in label
