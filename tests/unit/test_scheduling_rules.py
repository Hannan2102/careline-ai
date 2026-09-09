"""Scheduling primitives: clinic hours, the slot grid, and duration fitting."""

from __future__ import annotations

from datetime import UTC, date, datetime

from app.utils.scheduling import (
    covering_grid_starts,
    date_range,
    fits_within_working_hours,
    grid_starts,
    intervals_overlap,
    is_working_day,
    slot_id_for,
    working_intervals,
)

TUESDAY = date(2026, 9, 15)
SATURDAY = date(2026, 9, 19)
SUNDAY = date(2026, 9, 20)


def test_clinic_is_closed_at_weekends() -> None:
    assert is_working_day(TUESDAY)
    assert not is_working_day(SATURDAY)
    assert not is_working_day(SUNDAY)
    assert grid_starts(SATURDAY) == []


def test_working_day_is_split_around_the_lunch_break() -> None:
    intervals = working_intervals(TUESDAY)
    assert len(intervals) == 2
    morning, afternoon = intervals
    # 08:00-12:00 and 13:00-17:00 America/New_York, expressed in UTC.
    assert morning[0].hour == 12 and morning[1].hour == 16
    assert afternoon[0].hour == 17 and afternoon[1].hour == 21


def test_grid_covers_eight_hours_at_fifteen_minute_spacing() -> None:
    starts = grid_starts(TUESDAY)
    assert len(starts) == 32  # 8 open hours / 15 minutes
    assert all(s.tzinfo is UTC for s in starts)
    assert starts == sorted(starts)


def test_appointment_may_not_straddle_the_lunch_break() -> None:
    """11:45 local + 45 minutes would run into the break, so it must not fit."""
    late_morning = datetime(2026, 9, 15, 15, 45, tzinfo=UTC)  # 11:45 EDT
    assert fits_within_working_hours(late_morning, 15)
    assert not fits_within_working_hours(late_morning, 45)


def test_appointment_may_not_run_past_closing() -> None:
    last_start = grid_starts(TUESDAY)[-1]  # 16:45 EDT
    assert fits_within_working_hours(last_start, 15)
    assert not fits_within_working_hours(last_start, 20)


def test_duration_rounds_up_onto_the_grid() -> None:
    """A 20-minute visit occupies two 15-minute grid slots (documented tradeoff)."""
    start = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
    assert len(covering_grid_starts(start, 15)) == 1
    assert len(covering_grid_starts(start, 20)) == 2
    assert len(covering_grid_starts(start, 30)) == 2
    assert len(covering_grid_starts(start, 45)) == 3


def test_touching_intervals_do_not_overlap() -> None:
    a = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
    b = datetime(2026, 9, 15, 12, 30, tzinfo=UTC)
    c = datetime(2026, 9, 15, 13, 0, tzinfo=UTC)
    assert not intervals_overlap(a, b, b, c)
    assert intervals_overlap(a, c, b, c)


def test_slot_ids_are_stable_and_readable() -> None:
    start = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
    assert slot_id_for("Practitioner/prac-sarah-patel", start) == "slot-patel-20260915-1200"


def test_date_range_is_inclusive_and_tolerates_inversion() -> None:
    assert len(date_range(date(2026, 9, 14), date(2026, 9, 18))) == 5
    assert date_range(date(2026, 9, 18), date(2026, 9, 14)) == []
