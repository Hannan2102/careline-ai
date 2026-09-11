"""Human-readable formatting of stored values.

Times are stored in UTC and spoken in clinic-local time. Getting that
conversion wrong would offer a patient an appointment at the wrong hour, so it
happens in one place.
"""

from __future__ import annotations

import re
from datetime import datetime

from app.config.clinic import CLINIC_TIMEZONE

#: The days as a caller says them, and as ``format_day`` writes them.
#:
#: Here rather than in the extractor because both the extractor and the
#: workflows need to know whether a caller named a day, and a workflow
#: importing the extractor's internals to find out would be the wrong way
#: round -- workflows take typed input and know nothing about parsing.
WEEKDAYS: tuple[str, ...] = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)

_WEEKDAY_RE = re.compile(r"\b(?:" + "|".join(WEEKDAYS) + r")s?\b", re.IGNORECASE)


def mentions_a_weekday(text: str) -> bool:
    """Whether the caller named a day of the week."""
    return _WEEKDAY_RE.search(text) is not None


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
