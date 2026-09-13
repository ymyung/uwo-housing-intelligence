"""Deterministic service-coverage inspection for static GTFS archives."""

from __future__ import annotations

import csv
import hashlib
import io
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any


class GtfsValidationError(ValueError):
    """A GTFS archive is unsafe, incomplete, or internally invalid."""


REQUIRED_GTFS_FILES = frozenset(
    {"agency.txt", "stops.txt", "routes.txt", "trips.txt", "stop_times.txt"}
)
WEEKDAY_COLUMNS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


def _gtfs_date(value: str | None, *, field: str) -> date:
    try:
        return datetime.strptime(str(value or "").strip(), "%Y%m%d").date()
    except ValueError as exc:
        raise GtfsValidationError(f"GTFS {field} contains an invalid date") from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _rows(archive: zipfile.ZipFile, entries: dict[str, str], name: str) -> list[dict[str, str]]:
    entry = entries.get(name)
    if not entry:
        return []
    try:
        text = archive.read(entry).decode("utf-8-sig")
    except (KeyError, UnicodeDecodeError) as exc:
        raise GtfsValidationError(f"GTFS {name} is unreadable") from exc
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if not reader.fieldnames:
        raise GtfsValidationError(f"GTFS {name} has no header")
    return [dict(row) for row in reader]


@dataclass(frozen=True)
class GtfsFreshnessReport:
    feed_sha256: str
    feed_publisher_name: str | None
    feed_publisher_url: str | None
    feed_version: str | None
    feed_declared_start_date: date | None
    feed_declared_end_date: date | None
    service_start_date: date
    service_end_date: date
    active_service_date_count: int
    reference_week_start: date
    reference_dates: tuple[date, ...]
    reference_week_supported: bool
    as_of_date: date
    expired: bool

    @property
    def schedule_version(self) -> str:
        return f"gtfs-sha256-{self.feed_sha256[:12]}"

    @property
    def graph_rebuild_recommended(self) -> bool:
        return self.expired or not self.reference_week_supported

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "feed_sha256": self.feed_sha256,
            "schedule_version": self.schedule_version,
            "feed_publisher_name": self.feed_publisher_name,
            "feed_publisher_url": self.feed_publisher_url,
            "feed_version": self.feed_version,
            "feed_declared_start_date": (
                self.feed_declared_start_date.isoformat()
                if self.feed_declared_start_date
                else None
            ),
            "feed_declared_end_date": (
                self.feed_declared_end_date.isoformat()
                if self.feed_declared_end_date
                else None
            ),
            "service_start_date": self.service_start_date.isoformat(),
            "service_end_date": self.service_end_date.isoformat(),
            "active_service_date_count": self.active_service_date_count,
            "reference_week_start": self.reference_week_start.isoformat(),
            "reference_dates": [value.isoformat() for value in self.reference_dates],
            "reference_week_supported": self.reference_week_supported,
            "as_of_date": self.as_of_date.isoformat(),
            "expired": self.expired,
            "graph_rebuild_recommended": self.graph_rebuild_recommended,
            "transit_data_kind": "static_schedule",
            "live_departures_supported": False,
        }


def inspect_gtfs_feed(
    path: Path,
    *,
    reference_week_start: date,
    as_of_date: date | None = None,
) -> GtfsFreshnessReport:
    """Inspect actual service calendars rather than trusting filenames."""

    path = path.resolve()
    if reference_week_start.weekday() != 0:
        raise GtfsValidationError("reference week must start on Monday")
    if not path.is_file():
        raise GtfsValidationError(f"GTFS archive is missing: {path}")
    try:
        with zipfile.ZipFile(path) as archive:
            if archive.testzip() is not None:
                raise GtfsValidationError("GTFS archive contains a corrupt member")
            entries: dict[str, str] = {}
            for info in archive.infolist():
                normalized = info.filename.replace("\\", "/")
                if normalized.startswith("/") or ".." in Path(normalized).parts:
                    raise GtfsValidationError("GTFS archive contains an unsafe path")
                entries.setdefault(Path(normalized).name.casefold(), info.filename)
            missing = sorted(REQUIRED_GTFS_FILES - entries.keys())
            if missing:
                raise GtfsValidationError(
                    "GTFS archive is missing required files: " + ", ".join(missing)
                )
            calendar = _rows(archive, entries, "calendar.txt")
            exceptions = _rows(archive, entries, "calendar_dates.txt")
            feed_rows = _rows(archive, entries, "feed_info.txt")
    except (OSError, zipfile.BadZipFile) as exc:
        raise GtfsValidationError("GTFS input is not a valid ZIP archive") from exc

    if not calendar and not exceptions:
        raise GtfsValidationError(
            "GTFS requires calendar.txt or calendar_dates.txt service data"
        )
    service_by_date: dict[date, set[str]] = {}
    for row in calendar:
        service_id = str(row.get("service_id") or "").strip()
        if not service_id:
            raise GtfsValidationError("GTFS calendar service_id is required")
        start = _gtfs_date(row.get("start_date"), field="calendar.start_date")
        end = _gtfs_date(row.get("end_date"), field="calendar.end_date")
        if end < start or (end - start).days > 3660:
            raise GtfsValidationError("GTFS calendar date range is invalid")
        for offset in range((end - start).days + 1):
            current = start + timedelta(days=offset)
            if str(row.get(WEEKDAY_COLUMNS[current.weekday()]) or "0").strip() == "1":
                service_by_date.setdefault(current, set()).add(service_id)
    for row in exceptions:
        service_id = str(row.get("service_id") or "").strip()
        current = _gtfs_date(row.get("date"), field="calendar_dates.date")
        exception_type = str(row.get("exception_type") or "").strip()
        if not service_id or exception_type not in {"1", "2"}:
            raise GtfsValidationError("GTFS calendar_dates row is invalid")
        services = service_by_date.setdefault(current, set())
        if exception_type == "1":
            services.add(service_id)
        else:
            services.discard(service_id)
    active_dates = sorted(value for value, services in service_by_date.items() if services)
    if not active_dates:
        raise GtfsValidationError("GTFS service calendars contain no active dates")

    feed_info = feed_rows[0] if feed_rows else {}
    declared_start = (
        _gtfs_date(feed_info.get("feed_start_date"), field="feed_start_date")
        if str(feed_info.get("feed_start_date") or "").strip()
        else None
    )
    declared_end = (
        _gtfs_date(feed_info.get("feed_end_date"), field="feed_end_date")
        if str(feed_info.get("feed_end_date") or "").strip()
        else None
    )
    # These are the service days exercised by the six configured periods.
    reference_dates = (
        reference_week_start,
        reference_week_start + timedelta(days=5),
        reference_week_start + timedelta(days=6),
    )
    active_set = set(active_dates)
    today = as_of_date or date.today()
    return GtfsFreshnessReport(
        feed_sha256=_sha256(path),
        feed_publisher_name=feed_info.get("feed_publisher_name") or None,
        feed_publisher_url=feed_info.get("feed_publisher_url") or None,
        feed_version=feed_info.get("feed_version") or None,
        feed_declared_start_date=declared_start,
        feed_declared_end_date=declared_end,
        service_start_date=active_dates[0],
        service_end_date=active_dates[-1],
        active_service_date_count=len(active_dates),
        reference_week_start=reference_week_start,
        reference_dates=reference_dates,
        reference_week_supported=all(value in active_set for value in reference_dates),
        as_of_date=today,
        expired=active_dates[-1] < today,
    )
