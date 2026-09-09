"""Intents the orchestrator can route to.

One per workflow, plus the two outcomes that are not workflows: a request for a
human, and not knowing. ``UNKNOWN`` is a real answer -- guessing a workflow
from an unclear request is how a caller ends up cancelling an appointment they
meant to ask about.
"""

from __future__ import annotations

from enum import StrEnum


class Intent(StrEnum):
    BOOK_APPOINTMENT = "book_appointment"
    LOOKUP_APPOINTMENT = "lookup_appointment"
    CANCEL_APPOINTMENT = "cancel_appointment"
    RESCHEDULE_APPOINTMENT = "reschedule_appointment"
    MEDICATION_LOOKUP = "medication_lookup"
    REFILL_REQUEST = "refill_request"
    CLINIC_FAQ = "clinic_faq"
    HUMAN_REQUESTED = "human_requested"
    UNKNOWN = "unknown"
