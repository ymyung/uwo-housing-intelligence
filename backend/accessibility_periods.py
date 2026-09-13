"""Configurable representative periods for transit profile sampling."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time

from backend.domain import DayType, TimePeriod


@dataclass(frozen=True)
class TransitPeriodDefinition:
    id: TimePeriod
    label: str
    day_type: DayType
    departures: tuple[time, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id.value,
            "label": self.label,
            "day_type": self.day_type.value,
            "departures": [departure.strftime("%H:%M") for departure in self.departures],
        }


def _times(*values: str) -> tuple[time, ...]:
    return tuple(time.fromisoformat(value) for value in values)


DEFAULT_TRANSIT_PERIODS = (
    TransitPeriodDefinition(
        TimePeriod.WEEKDAY_MORNING_COMMUTE,
        "Weekday morning",
        DayType.WEEKDAY,
        _times("07:30", "08:00", "08:30"),
    ),
    TransitPeriodDefinition(
        TimePeriod.WEEKDAY_MIDDAY,
        "Weekday midday",
        DayType.WEEKDAY,
        _times("11:30", "12:00", "12:30"),
    ),
    TransitPeriodDefinition(
        TimePeriod.WEEKDAY_EVENING_COMMUTE,
        "Weekday evening",
        DayType.WEEKDAY,
        _times("16:30", "17:00", "17:30"),
    ),
    TransitPeriodDefinition(
        TimePeriod.WEEKDAY_LATE_EVENING,
        "Weekday late evening",
        DayType.WEEKDAY,
        _times("21:30", "22:00", "22:30"),
    ),
    TransitPeriodDefinition(
        TimePeriod.SATURDAY_DAYTIME,
        "Saturday daytime",
        DayType.SATURDAY,
        _times("11:30", "12:00", "12:30"),
    ),
    TransitPeriodDefinition(
        TimePeriod.SUNDAY_DAYTIME,
        "Sunday daytime",
        DayType.SUNDAY,
        _times("11:30", "12:00", "12:30"),
    ),
)

PERIOD_BY_ID = {period.id: period for period in DEFAULT_TRANSIT_PERIODS}
