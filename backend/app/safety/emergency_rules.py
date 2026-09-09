"""Potentially urgent symptoms.

The one category where the agent speaks before it escalates: it directs the
caller to emergency services, says plainly that it cannot help clinically, and
raises an urgent escalation. It never assesses severity, and it never
reassures -- "that sounds like nothing to worry about" is a clinical judgement.

Detection is a deliberately blunt phrase list. Over-triggering here costs a
caller one redirected sentence; under-triggering costs something else entirely,
so the list errs toward firing.
"""

from __future__ import annotations

import re

from app.safety.models import SafetyCategory, SafetyDecision, refusal

RULE_NAME = "emergency.red_flag_symptoms"

EMERGENCY_MESSAGE = (
    "If this might be an emergency, please hang up and call 911 right away, or go to "
    "your nearest emergency room. I'm not able to help with medical concerns myself, "
    "but I've flagged this for our clinical staff immediately."
)

#: Literal phrases that route a caller to emergency services. Grouped by what
#: they indicate so the list stays reviewable by a clinician.
RED_FLAG_PHRASES: tuple[str, ...] = (
    # Cardiac
    "crushing chest",
    "pain in my chest",
    "tightness in my chest",
    "heart attack",
    # Respiratory
    "trouble breathing",
    "difficulty breathing",
    "struggling to breathe",
    "gasping",
    # Neurological / stroke
    "stroke",
    "face is drooping",
    "face drooping",
    "slurred speech",
    "can't speak",
    "numbness on one side",
    "weakness on one side",
    "worst headache",
    "unconscious",
    "unresponsive",
    "seizure",
    "convulsing",
    # Bleeding / trauma
    "severe bleeding",
    "bleeding heavily",
    "won't stop bleeding",
    "coughing up blood",
    "vomiting blood",
    # Allergic
    "throat is closing",
    "throat closing",
    "anaphylaxis",
    "anaphylactic",
    "lips are swelling",
    "tongue is swelling",
    # Overdose
    "took too many",
)

#: Patterns for phrases that vary by inflection or wording. A literal list is
#: not enough here: "end my life" and "ending my life" mean the same thing, and
#: missing the second because of a suffix is the worst failure this module can
#: have. Self-harm and airway phrasing therefore get patterns, not phrases.
RED_FLAG_PATTERNS: tuple[str, ...] = (
    # Self-harm / suicidal ideation
    r"\b(end|ending|ends)\s+(my|his|her|their)\s+(own\s+)?li(fe|ves)\b",
    r"\b(kill|killing|kills)\s+(myself|himself|herself|themsel(f|ves))\b",
    r"\b(hurt|hurting|harm|harming)\s+(myself|himself|herself|themsel(f|ves))\b",
    r"\btak(e|ing)\s+(my|his|her|their)\s+own\s+life\b",
    r"\bwant(ing|s)?\s+to\s+die\b",
    r"\b(don'?t|do not|doesn'?t)\s+want\s+to\s+(live|be here|carry on)\b",
    r"\bbetter off dead\b",
    r"\bno reason to (live|go on)\b",
    r"\bsuicid(e|al)\b",
    # Overdose
    r"\boverdos(e|ed|ing)\b",
    # Cardiac / respiratory / consciousness, allowing for inflection
    r"\bchest\s+(pain|pains|tightness|pressure)\b",
    r"\b(can'?t|cannot|couldn'?t|could not|unable to)\s+breathe?\b",
    r"\bpass(ed|ing)\s+out\b",
    r"\bblack(ed|ing)\s+out\b",
)

_PATTERN = re.compile(
    "|".join(
        [
            r"(?<!\w)(?:" + "|".join(re.escape(p) for p in RED_FLAG_PHRASES) + r")(?!\w)",
            *RED_FLAG_PATTERNS,
        ]
    ),
    re.IGNORECASE,
)


def check_emergency(utterance: str) -> SafetyDecision | None:
    """Refuse and escalate urgently if the utterance contains a red flag."""
    match = _PATTERN.search(utterance)
    if match is None:
        return None
    return refusal(
        category=SafetyCategory.URGENT_SYMPTOMS,
        rule=RULE_NAME,
        message=EMERGENCY_MESSAGE,
        matched_text=match.group(0),
        rationale=(
            f"red-flag phrase {match.group(0)!r}; directed to emergency services "
            "without assessing severity"
        ),
    )
