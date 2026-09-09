"""Human-readable formatting of stored values.

Times are stored in UTC and spoken in clinic-local time. Getting that
conversion wrong would offer a patient an appointment at the wrong hour, so it
happens in one place.
"""

from __future__ import annotations

from datetime import datetime

from app.config.clinic import CLINIC_TIMEZONE


def local(value: datetime) -> datetime:
    """Convert a stored UTC datetime to clinic-local time."""
    return value.astimezone(CLINIC_TIMEZONE)


def format_time(value: datetime) -> str:
    """'9:00 AM' -- no leading zero, which reads badly aloud."""
    moment = local(value)
    return f"{moment.strftime('%I').lstrip('0')}:{moment.strftime('%M %p')}"


def format_day(value: datetime) -> str:
    """'Tuesday 15 September'."""
    moment = local(value)
    return f"{moment.strftime('%A')} {moment.strftime('%d').lstrip('0')} {moment.strftime('%B')}"


def format_slot(value: datetime) -> str:
    """'Tuesday 15 September at 9:00 AM'."""
    return f"{format_day(value)} at {format_time(value)}"


def format_appointment(start: datetime, practitioner_name: str) -> str:
    """'Tuesday 15 September at 9:00 AM with Dr. Sarah Patel'."""
    return f"{format_slot(start)} with {practitioner_name}"
