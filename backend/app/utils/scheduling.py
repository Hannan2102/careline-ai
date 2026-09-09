"""Scheduling primitives.

Pure functions shared by every EHR provider, so availability means the same
thing whether it is computed in memory or read from HAPI. Keeping them pure
also makes the awkward cases -- lunch breaks, weekends, durations that span
several grid slots -- directly unit-testable.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from app.config.clinic import (
    CLINIC_TIMEZONE,
    SLOT_GRID_MINUTES,
    STANDARD_HOURS,
    WORKING_DAYS,
)


def is_working_day(day: date) -> bool:
    return day.weekday() in WORKING_DAYS


def working_intervals(day: date) -> list[tuple[datetime, datetime]]:
    """Open periods for a day as UTC-aware intervals, split around the break."""
    if not is_working_day(day):
        return []

    def local(t: object) -> datetime:
        return datetime.combine(day, t, tzinfo=CLINIC_TIMEZONE).astimezone(UTC)  # type: ignore[arg-type]

    hours = STANDARD_HOURS
    if hours.break_start and hours.break_end:
        return [
            (local(hours.opens), local(hours.break_start)),
            (local(hours.break_end), local(hours.closes)),
        ]
    return [(local(hours.opens), local(hours.closes))]


def grid_starts(day: date) -> list[datetime]:
    """Every slot start on the grid for a day, in UTC."""
    step = timedelta(minutes=SLOT_GRID_MINUTES)
    starts: list[datetime] = []
    for open_at, close_at in working_intervals(day):
        cursor = open_at
        while cursor < close_at:
            starts.append(cursor)
            cursor += step
    return starts


def date_range(start: date, end: date) -> list[date]:
    """Inclusive list of dates. Returns empty when ``end`` precedes ``start``."""
    if end < start:
        return []
    return [start + timedelta(days=n) for n in range((end - start).days + 1)]


def fits_within_working_hours(start: datetime, duration_minutes: int) -> bool:
    """True when the whole appointment fits inside one open period.

    An appointment may not straddle the lunch break or the end of the day.
    """
    end = start + timedelta(minutes=duration_minutes)
    day = start.astimezone(CLINIC_TIMEZONE).date()
    return any(open_at <= start and end <= close_at for open_at, close_at in working_intervals(day))


def covering_grid_starts(start: datetime, duration_minutes: int) -> list[datetime]:
    """Grid slots consumed by an appointment.

    Availability is tracked on a fixed grid, so a 20-minute visit starting at
    09:00 occupies the 09:00 and 09:15 slots and the next bookable start is
    09:30. Durations round up to the grid; this is a documented simplification
    (docs/fhir-data-model.md).
    """
    step = timedelta(minutes=SLOT_GRID_MINUTES)
    end = start + timedelta(minutes=duration_minutes)
    covered: list[datetime] = []
    cursor = start
    while cursor < end:
        covered.append(cursor)
        cursor += step
    return covered


def slot_id_for(practitioner_ref: str, start: datetime) -> str:
    """Stable, readable slot id: 'slot-patel-20260915-1400' (UTC)."""
    who = practitioner_ref.rsplit("prac-", 1)[-1].split("-")[-1]
    return f"slot-{who}-{start.astimezone(UTC):%Y%m%d-%H%M}"


def intervals_overlap(
    a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime
) -> bool:
    """Half-open overlap: touching intervals do not conflict."""
    return a_start < b_end and b_start < a_end


def to_utc(value: datetime) -> datetime:
    """Normalise to UTC, treating a naive datetime as already UTC."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def clinic_date(moment: datetime) -> date:
    """The clinic-local calendar date of an instant.

    A 6pm appointment in New York is the *next* day in UTC, so filtering a
    schedule by UTC date would silently move evening appointments.
    """
    return to_utc(moment).astimezone(CLINIC_TIMEZONE).date()


def in_date_range(moment: datetime, start_date: date, end_date: date) -> bool:
    """Whether an instant falls inside an inclusive clinic-local date range."""
    return start_date <= clinic_date(moment) <= end_date
