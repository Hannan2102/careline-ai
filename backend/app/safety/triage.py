"""Question-type triage: diagnosis, treatment, and requests for a human.

The hardest part of this module is *not* over-blocking. Refusing "when is my
appointment?" as a clinical question would make the product useless, so the
patterns are deliberately narrow and every one of them has a negative test
alongside it. "do I have" is a good example: it must fire on "do I have an
infection" and stay silent on "do I have an appointment tomorrow".
"""

from __future__ import annotations

import re

from app.safety.models import SafetyCategory, SafetyDecision, refusal

DIAGNOSIS_RULE = "triage.diagnosis_request"
TREATMENT_RULE = "triage.treatment_request"
HUMAN_RULE = "triage.human_requested"

DIAGNOSIS_MESSAGE = (
    "I'm not able to answer medical questions like that — a clinician needs to. I've "
    "passed what you've told me to our clinical staff, and someone will get back to you."
)

TREATMENT_MESSAGE = (
    "I'm not able to advise on treatment — that needs a clinician. I've recorded your "
    "question and sent it to our clinical staff to review."
)

HUMAN_MESSAGE = (
    "Of course — I'll pass you to a member of our staff. I've made a note of what we've "
    "discussed so you won't need to start over."
)

#: Conditions and symptom nouns that make "do I have ..." a clinical question
#: rather than an administrative one.
_CLINICAL_NOUNS = (
    r"an? (infection|allergy|fever|virus|condition|disease|illness|rash|fracture|"
    r"concussion|migraine|uti|blood clot|tumou?r)"
    r"|covid|flu|influenza|strep|pneumonia|diabetes|cancer|asthma|anemia|anaemia"
    r"|high blood pressure|hypertension|something serious|something wrong"
)

DIAGNOSIS_PATTERNS: tuple[str, ...] = (
    r"what('?s| is) wrong with me",
    r"what do you think (i have|it is)",
    r"what do i have\b",
    rf"do i have ({_CLINICAL_NOUNS})",
    rf"(could|might) (it|this) be ({_CLINICAL_NOUNS})",
    r"is (it|this) serious",
    r"should i be (worried|concerned) about",
    r"(can you )?diagnose",
    r"what('?s| is) (causing|behind) (my|this|these)",
    r"is (this|that) normal\b",
)

TREATMENT_PATTERNS: tuple[str, ...] = (
    r"what should i take",
    r"should i take\b",
    r"what (can|should) i do (for|about) (my|this|these|the)",
    r"how (do|should) i treat",
    r"is it (safe|ok|okay|alright) to take",
    r"can i take .{0,40}\b(with|while|alongside|together with)\b",
    r"what (medicine|medication|drug|antibiotic)s? (should|do) i",
    r"do i need (antibiotics|medication|treatment|surgery)",
    r"(recommend|suggest) (a |any )?(medicine|medication|treatment|painkiller)",
    r"(is|are) (there )?anything i can take",
)

HUMAN_PATTERNS: tuple[str, ...] = (
    r"(speak|talk|chat) (to|with) (a |an |the )?(human|person|real person|someone|"
    r"somebody|nurse|doctor|receptionist|staff|member of staff|agent|operator)",
    r"(transfer|put) me (through|to)",
    r"(get|give) me (a|an) (human|person|nurse|real person)",
    r"i (want|need|would like) (to speak to )?(a |an )?(human|person|real person)",
    r"(is|can i get) (there )?a (human|person|real person)",
    r"stop talking to (a|the) (bot|robot|machine|computer)",
)

_DIAGNOSIS = re.compile("|".join(DIAGNOSIS_PATTERNS), re.IGNORECASE)
_TREATMENT = re.compile("|".join(TREATMENT_PATTERNS), re.IGNORECASE)
_HUMAN = re.compile("|".join(HUMAN_PATTERNS), re.IGNORECASE)


def check_diagnosis(utterance: str) -> SafetyDecision | None:
    match = _DIAGNOSIS.search(utterance)
    if match is None:
        return None
    return refusal(
        category=SafetyCategory.DIAGNOSIS_REQUEST,
        rule=DIAGNOSIS_RULE,
        message=DIAGNOSIS_MESSAGE,
        matched_text=match.group(0),
        rationale=f"diagnosis sought: {match.group(0)!r}; no clinical opinion offered",
    )


def check_treatment(utterance: str) -> SafetyDecision | None:
    match = _TREATMENT.search(utterance)
    if match is None:
        return None
    return refusal(
        category=SafetyCategory.TREATMENT_REQUEST,
        rule=TREATMENT_RULE,
        message=TREATMENT_MESSAGE,
        matched_text=match.group(0),
        rationale=f"treatment advice sought: {match.group(0)!r}",
    )


def check_human_requested(utterance: str) -> SafetyDecision | None:
    """A request for a person is honoured immediately, never negotiated."""
    match = _HUMAN.search(utterance)
    if match is None:
        return None
    return refusal(
        category=SafetyCategory.HUMAN_REQUESTED,
        rule=HUMAN_RULE,
        message=HUMAN_MESSAGE,
        matched_text=match.group(0),
        rationale="patient asked for a human",
    )
