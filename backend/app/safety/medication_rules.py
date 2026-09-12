"""Medication safety rules.

Two categories, both absolute:

* **Dose modification** -- taking more, less, half, or stopping. The agent may
  read back what a clinician prescribed (SAFETY.md category A), but changing
  how it is taken is a clinical act.
* **Side effects** -- a symptom attributed to a medication. Answering means
  judging whether the drug caused it, which is diagnosis.

The refusal text is fixed and contains no dosing language, so the response
cannot itself become the advice being refused.
"""

from __future__ import annotations

import re

from app.safety.models import SafetyCategory, SafetyDecision, refusal

DOSE_RULE = "medication.dose_modification"
SIDE_EFFECT_RULE = "medication.side_effect"

DOSE_MESSAGE = (
    "I'm not able to advise on changing a medication — that needs a clinician. I've "
    "recorded what you told me and passed it to our clinical staff to review, and "
    "someone will get back to you."
)

SIDE_EFFECT_MESSAGE = (
    "I'm not able to give advice about how a medication is affecting you. I've recorded "
    "your concern and sent it to our clinical staff to review, and someone will get back "
    "to you. If you feel unwell right now, please contact your clinician or emergency "
    "services."
)

#: Asking to change how a medication is taken.
DOSE_CHANGE_PATTERNS: tuple[str, ...] = (
    r"take half",
    r"half (a |the |my )?(pill|tablet|dose)",
    r"cut (it |them |the pill|the tablet|my pill)?\s*in half",
    r"split (the |my )?(pill|tablet|dose)",
    r"(take|have) (two|three|double|extra|another|an extra|more)\b",
    # Not "double the dose" -- just "double". Callers name the medicine the
    # way they hold it, so "should I double my blood pressure tablets?" never
    # contains the word this rule used to require, and it was allowed through
    # to be answered as an ordinary question. Found while testing something
    # else, which is the only reason it was found.
    #
    # Two exclusions, because they are the only innocent uses in this domain.
    # Everything else the word could mean here is a dose change, and the cost
    # of being wrong runs one way: a needless transfer to a nurse against
    # telling somebody to take twice their blood pressure medication.
    r"\b(double|doubling|triple|tripling|halve|halving)\b(?!\s+(check|checking|book|booking))",
    r"(increase|decrease|lower|raise|reduce|change|adjust) (the |my )?(dose|dosage)",
    r"(take|use) (it |them |my medication )?(less|more) often",
    r"skip (a |my |the )?(dose|pill|tablet)",
    r"stop (taking|using)",
    r"come off (my |the )?(medication|meds)",
    r"(twice|three times) (a day )?instead",
    r"instead of (one|two|the) (pill|tablet|dose)",
)

#: A symptom attributed to a medication.
SIDE_EFFECT_PATTERNS: tuple[str, ...] = (
    r"(is |are )?(making|makes) me (feel )?\w+",
    r"side[- ]effects?",
    r"(bad |allergic )?reaction to (my |the )?\w+",
    r"since I started (taking|on)",
    r"(is |it's |its )?(giving|gives) me \w+",
    r"(causing|caused) (my |me )?\w+",
    r"(feel|feeling) (dizzy|sick|nauseous|awful|terrible|unwell|weird|off)"
    r" (after|when I take|since)",
    r"(after|when) I take (it|my|the)\b.{0,40}(dizzy|sick|nauseous|tired|unwell)",
)

_DOSE = re.compile("|".join(DOSE_CHANGE_PATTERNS), re.IGNORECASE)
_SIDE_EFFECT = re.compile("|".join(SIDE_EFFECT_PATTERNS), re.IGNORECASE)


def check_dose_modification(utterance: str) -> SafetyDecision | None:
    match = _DOSE.search(utterance)
    if match is None:
        return None
    return refusal(
        category=SafetyCategory.DOSE_MODIFICATION,
        rule=DOSE_RULE,
        message=DOSE_MESSAGE,
        matched_text=match.group(0),
        rationale=f"dose-change request {match.group(0)!r}; no dosing guidance given",
    )


def check_side_effect(utterance: str) -> SafetyDecision | None:
    match = _SIDE_EFFECT.search(utterance)
    if match is None:
        return None
    return refusal(
        category=SafetyCategory.MEDICATION_SIDE_EFFECT,
        rule=SIDE_EFFECT_RULE,
        message=SIDE_EFFECT_MESSAGE,
        matched_text=match.group(0),
        rationale=f"symptom attributed to a medication: {match.group(0)!r}",
    )
