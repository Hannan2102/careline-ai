"""Oakwood Family Medicine - the fictional clinic.

Structured, deterministic clinic facts. Simple lookups ("are you open on
Saturday?") are answered from this table rather than from a model or a vector
store: it is faster, free, and cannot be wrong (docs/call-flows.md).
"""

from __future__ import annotations

from datetime import time
from zoneinfo import ZoneInfo

from app.schemas.domain import ClinicLocation, Practitioner, WorkingHours

#: All stored datetimes are timezone-aware UTC; clinic hours are expressed in
#: this zone and converted at the boundary, so a booking never drifts by an hour.
CLINIC_TIMEZONE = ZoneInfo("America/New_York")

CLINIC_NAME = "Oakwood Family Medicine"
CLINIC_DISPLAY_NAME = "CareLine AI at Oakwood Family Medicine"

#: What the caller hears before they have said anything.
#:
#: The agent speaks first for the same reason a receptionist does: a line that
#: opens in silence leaves the caller guessing whether it connected, and the
#: ones who guess wrong say "hello?" -- which carries no intent, so the first
#: real turn is wasted on a menu. Opening with the clinic's name also tells
#: someone who misdialled, before they have read out a date of birth.
#:
#: It says it is automated. That is not decoration: a caller who believes they
#: reached a person will ask a person's questions, and being told afterwards is
#: worse than being told now.
GREETING = (
    f"Thank you for calling {CLINIC_NAME}. "
    "You're speaking with CareLine, an automated assistant. "
    "How can I help you today?"
)

#: Monday=0 ... Sunday=6. Closed days are simply absent.
WORKING_DAYS: frozenset[int] = frozenset({0, 1, 2, 3, 4})

STANDARD_HOURS = WorkingHours(
    opens=time(8, 0),
    closes=time(17, 0),
    break_start=time(12, 0),
    break_end=time(13, 0),
)

#: Availability is computed on this grid; appointments round up to it
#: (docs/fhir-data-model.md).
SLOT_GRID_MINUTES = 15

CLINIC_LOCATION = ClinicLocation(
    reference="Location/loc-oakwood",
    name=CLINIC_NAME,
    address_line="1420 Oakwood Avenue, Suite 200",
    city="Riverton",
    state="OH",
    postal_code="45042",
    phone="(555) 019-2200",
)

PRACTITIONERS: tuple[Practitioner, ...] = (
    Practitioner(
        reference="Practitioner/prac-sarah-patel",
        given_name="Sarah",
        family_name="Patel",
        specialty="Family Medicine",
    ),
    Practitioner(
        reference="Practitioner/prac-michael-johnson",
        given_name="Michael",
        family_name="Johnson",
        specialty="Internal Medicine",
    ),
    Practitioner(
        reference="Practitioner/prac-emily-chen",
        given_name="Emily",
        family_name="Chen",
        specialty="Family Medicine",
    ),
)

PRACTITIONERS_BY_REF: dict[str, Practitioner] = {p.reference: p for p in PRACTITIONERS}


def schedule_ref_for(practitioner_ref: str) -> str:
    """Schedule id for a practitioner, e.g. 'Practitioner/prac-x' -> 'Schedule/sched-x'."""
    return "Schedule/sched-" + practitioner_ref.rsplit("prac-", 1)[-1]


def find_practitioner_by_name(name: str) -> Practitioner | None:
    """Resolve a spoken name ("Dr. Patel", "patel", "Sarah Patel") to a practitioner.

    Returns ``None`` when the name is absent or ambiguous -- the caller escalates
    rather than guessing which clinician was meant.
    """
    needle = name.lower().replace("dr.", "").replace("dr ", "").strip()
    if not needle:
        return None
    matches = [
        p
        for p in PRACTITIONERS
        if needle in p.family_name.lower() or needle in f"{p.given_name} {p.family_name}".lower()
    ]
    return matches[0] if len(matches) == 1 else None


# --------------------------------------------------------------------------
# FAQ knowledge -- deterministic, no RAG (see DEMO.md, docs/call-flows.md)
# --------------------------------------------------------------------------

ACCEPTED_INSURANCE: tuple[str, ...] = (
    "Blue Shield PPO (demo)",
    "Meridian Health HMO (demo)",
    "Statewide Medicaid (demo)",
    "Medicare Part B (demo)",
    "Oakwood Employee Plan (demo)",
)

CLINIC_FAQ: dict[str, str] = {
    "hours": (
        "We're open Monday through Friday, 8:00 AM to 5:00 PM, and closed on Saturday and Sunday."
    ),
    "location": (
        f"We're at {CLINIC_LOCATION.address_line}, {CLINIC_LOCATION.city}, "
        f"{CLINIC_LOCATION.state} {CLINIC_LOCATION.postal_code}."
    ),
    "phone": f"Our main number is {CLINIC_LOCATION.phone}.",
    "parking": (
        "There's free patient parking in the lot behind the building, and the "
        "entrance is on the ground floor."
    ),
    "arrival_time": (
        "Please arrive about 15 minutes early, or 20 minutes if it's your first visit with us."
    ),
    "cancellation_policy": (
        "We ask for at least 24 hours' notice to cancel or reschedule so we can "
        "offer the time to someone else."
    ),
    "what_to_bring": (
        "Bring a photo ID, your insurance card, and a list of any medications "
        "you're currently taking."
    ),
    "new_patient_process": (
        "New patients get a 45-minute first visit. We'll take your details over "
        "the phone, and there's a short intake form to complete when you arrive."
    ),
    "insurance": "We accept " + ", ".join(ACCEPTED_INSURANCE) + ".",
    "billing": (
        "I can't quote what a visit will cost you, because that depends on your "
        "plan and your remaining deductible. Our front desk can check that "
        "against your cover and give you a figure before you come in."
    ),
    "providers": (
        "Our providers are "
        + ", ".join(f"{p.display_name} ({p.specialty})" for p in PRACTITIONERS)
        + "."
    ),
}

#: Spoken phrasings mapped to FAQ topics. Deliberately narrow: an unmatched
#: question escalates to the front desk instead of being improvised.
FAQ_TOPIC_ALIASES: dict[str, str] = {
    "hours": "hours",
    "open": "hours",
    "opening_hours": "hours",
    "weekend": "hours",
    "saturday": "hours",
    "sunday": "hours",
    "location": "location",
    "address": "location",
    "directions": "location",
    # Matching is substring-based, so an alias only fires on its exact letters:
    # "location" is not in "where are you located?", which left the single most
    # obvious phrasing of the question unanswered.
    "located": "location",
    "where_are_you": "location",
    "how_do_i_get_to_you": "location",
    "phone": "phone",
    "number": "phone",
    "parking": "parking",
    "arrival": "arrival_time",
    "arrival_time": "arrival_time",
    "early": "arrival_time",
    "cancel": "cancellation_policy",
    "cancellation": "cancellation_policy",
    "cancellation_policy": "cancellation_policy",
    "bring": "what_to_bring",
    "what_to_bring": "what_to_bring",
    "new_patient": "new_patient_process",
    "new_patient_process": "new_patient_process",
    "insurance": "insurance",
    "coverage": "insurance",
    # Money, which is not the same question as which plans are accepted.
    # Deliberately narrow substrings: matching is `in`, not whole-word, so
    # "fee" would fire on "I feel dizzy" and "charge" on "discharge" -- both
    # clinical utterances, and neither a billing question.
    "copay": "billing",
    "co_pay": "billing",
    "deductible": "billing",
    "out_of_pocket": "billing",
    "how_much_will_it_cost": "billing",
    "how_much_does_it_cost": "billing",
    "how_much_will_this_cost": "billing",
    "how_much_do_you_charge": "billing",
    "cost_me": "billing",
    "the_cost": "billing",
    "price": "billing",
    "billing": "billing",
    "my_bill": "billing",
    "insured": "insurance",
    "covered": "insurance",
    # The carrier as people say it, not as the plan is filed: nobody asks "do
    # you take Blue Shield PPO (demo)". These track ACCEPTED_INSURANCE above --
    # add the spoken form here when a plan is added there. A carrier we do not
    # accept matches nothing and ends up with the front desk, which is the
    # right place for it.
    "blue_shield": "insurance",
    "meridian": "insurance",
    "medicaid": "insurance",
    "medicare": "insurance",
    "employee_plan": "insurance",
    "providers": "providers",
    "doctors": "providers",
}


def lookup_faq(topic: str) -> str | None:
    """Return the answer for a topic, or ``None`` if we do not have one."""
    key = topic.strip().lower().replace(" ", "_")
    canonical = FAQ_TOPIC_ALIASES.get(key, key)
    return CLINIC_FAQ.get(canonical)
