"""Patient identity verification (ADR 003).

Nothing patient-specific is disclosed before verification -- including whether
a record exists at all. That last point drives most of the design here: the
difference between "no such patient" and "that patient exists but you gave the
wrong date of birth" is itself information, so both produce the same outcome
and the same message.

The rules:

* exactly one match on name + date of birth  -> verified
* zero matches                               -> nothing disclosed, attempt counted
* several matches                            -> a second factor is requested,
  and the candidates are never enumerated, hinted at, or narrowed aloud
* repeated failure                           -> locked out, escalated to a human

Verification is scoped to the session and dies with it.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from app.agents.state import SessionState, VerificationDecision
from app.observability.logging import get_logger
from app.schemas.domain import EscalationCategory, Patient, VerificationState
from app.services.base import ValidationError
from app.services.escalation_service import EscalationService
from app.services.patient_service import PatientService

logger = get_logger(__name__)

#: Attempts allowed before the caller is handed to a human. Three is enough for
#: a mis-heard surname over the phone without becoming an enumeration oracle.
MAX_VERIFICATION_ATTEMPTS = 3


class SecondFactorType(StrEnum):
    """Additional evidence accepted when the primary factors are ambiguous."""

    PHONE_LAST_FOUR = "phone_last_four"
    POSTAL_CODE = "postal_code"


class VerificationOutcome(StrEnum):
    VERIFIED = "VERIFIED"
    #: No single record matched. Deliberately does not distinguish "unknown
    #: patient" from "wrong details" -- that distinction is a disclosure.
    NO_MATCH = "NO_MATCH"
    SECOND_FACTOR_REQUIRED = "SECOND_FACTOR_REQUIRED"
    SECOND_FACTOR_FAILED = "SECOND_FACTOR_FAILED"
    LOCKED_OUT = "LOCKED_OUT"
    #: The session already failed; further attempts are not accepted.
    ALREADY_LOCKED = "ALREADY_LOCKED"


class VerificationResult(BaseModel):
    """What happened, and what the caller may be told.

    ``patient`` is populated only on success. Nothing in this object reveals
    anything about records that were not matched.
    """

    model_config = ConfigDict(frozen=True)

    outcome: VerificationOutcome
    patient: Patient | None = None
    #: Which second factor to ask for, when one is required.
    required_factor: SecondFactorType | None = None
    attempts_remaining: int = 0
    escalation_id: str | None = None

    @property
    def is_verified(self) -> bool:
        return self.outcome is VerificationOutcome.VERIFIED


class VerificationService:
    """Decides whether a session may access a patient's records."""

    def __init__(
        self,
        patients: PatientService,
        escalations: EscalationService,
        max_attempts: int = MAX_VERIFICATION_ATTEMPTS,
    ) -> None:
        self.patients = patients
        self.escalations = escalations
        self.max_attempts = max_attempts

    # --------------------------------------------------------------- step 1
    async def verify_identity(
        self, session: SessionState, full_name: str, date_of_birth: date
    ) -> VerificationResult:
        """Attempt verification from name and date of birth."""
        if session.is_locked_out:
            return VerificationResult(outcome=VerificationOutcome.ALREADY_LOCKED)

        try:
            candidates = await self.patients.find_candidates(full_name, date_of_birth)
        except ValidationError:
            # Malformed details are a failed attempt, not an error to explain:
            # explaining which part was malformed helps an attacker probe.
            return self._fail(session, VerificationOutcome.NO_MATCH, "invalid identity details")

        if not candidates:
            return self._fail(session, VerificationOutcome.NO_MATCH, "no matching record")

        if len(candidates) == 1:
            return self._succeed(session, candidates[0])

        # Several records share these details. Hold them privately and ask for
        # more evidence; never say how many, or how they differ.
        session.apply_verification(
            VerificationDecision(
                state=VerificationState.PENDING_SECOND_FACTOR,
                candidate_refs=tuple(c.reference for c in candidates),
                rationale=f"{len(candidates)} candidates matched name and date of birth",
            )
        )
        logger.info(
            "verification_second_factor_required",
            session_id=session.session_id,
            candidate_count=len(candidates),
        )
        return VerificationResult(
            outcome=VerificationOutcome.SECOND_FACTOR_REQUIRED,
            required_factor=SecondFactorType.PHONE_LAST_FOUR,
            attempts_remaining=self._remaining(session),
        )

    # --------------------------------------------------------------- step 2
    async def submit_second_factor(
        self, session: SessionState, factor: SecondFactorType, value: str
    ) -> VerificationResult:
        """Resolve an ambiguous match with additional evidence."""
        if session.is_locked_out:
            return VerificationResult(outcome=VerificationOutcome.ALREADY_LOCKED)
        if session.verification is not VerificationState.PENDING_SECOND_FACTOR:
            raise ValidationError(
                "no second factor is outstanding for this session; verify identity first"
            )

        supplied = value.strip()
        if not supplied:
            return self._fail(
                session, VerificationOutcome.SECOND_FACTOR_FAILED, "empty second factor"
            )

        matches: list[Patient] = []
        for reference in session.candidate_refs:
            patient = await self.patients.get_patient(reference)
            if self._factor_matches(patient, factor, supplied):
                matches.append(patient)

        if len(matches) == 1:
            return self._succeed(session, matches[0])

        # Zero matches and several matches are both failures, and are reported
        # identically: "your details matched two records, and so did the phone
        # number" is a disclosure.
        return self._fail(
            session,
            VerificationOutcome.SECOND_FACTOR_FAILED,
            f"second factor matched {len(matches)} candidates",
            keep_candidates=True,
        )

    # ------------------------------------------------------------- internals
    @staticmethod
    def _factor_matches(patient: Patient, factor: SecondFactorType, value: str) -> bool:
        """Compare a supplied factor against the record.

        Comparison is tolerant of formatting -- a phone number read aloud and
        transcribed will not match character for character -- but not of value.
        """
        if factor is SecondFactorType.PHONE_LAST_FOUR:
            supplied = "".join(c for c in value if c.isdigit())[-4:]
            stored = patient.phone_last_four
            return bool(stored) and len(supplied) == 4 and supplied == stored
        if factor is SecondFactorType.POSTAL_CODE:
            stored_postal = (patient.postal_code or "").strip().upper().replace(" ", "")
            return bool(stored_postal) and value.upper().replace(" ", "") == stored_postal
        # No fallback branch on purpose: the checks above are exhaustive over
        # SecondFactorType, so adding a factor without handling it here becomes
        # a type error rather than a silent "does not match".

    def _succeed(self, session: SessionState, patient: Patient) -> VerificationResult:
        session.apply_verification(
            VerificationDecision(
                state=VerificationState.VERIFIED,
                patient_ref=patient.reference,
                rationale="single matching record",
            )
        )
        logger.info(
            "verification_succeeded",
            session_id=session.session_id,
            patient_ref=patient.reference,
        )
        return VerificationResult(
            outcome=VerificationOutcome.VERIFIED,
            patient=patient,
            attempts_remaining=self._remaining(session),
        )

    def _fail(
        self,
        session: SessionState,
        outcome: VerificationOutcome,
        rationale: str,
        keep_candidates: bool = False,
    ) -> VerificationResult:
        attempts = session.record_failed_attempt()
        logger.info(
            "verification_failed",
            session_id=session.session_id,
            reason=rationale,
            attempts=attempts,
        )

        if attempts >= self.max_attempts:
            session.apply_verification(
                VerificationDecision(
                    state=VerificationState.FAILED,
                    rationale=f"{attempts} failed attempts: {rationale}",
                )
            )
            escalation = self.escalations.create(
                category=EscalationCategory.FAILED_VERIFICATION,
                summary=(
                    f"Caller could not be verified after {attempts} attempts. "
                    "No patient information was disclosed."
                ),
                session_id=session.session_id,
                verification_state=VerificationState.FAILED,
                ai_action="No patient information disclosed",
            )
            return VerificationResult(
                outcome=VerificationOutcome.LOCKED_OUT,
                attempts_remaining=0,
                escalation_id=escalation.escalation_id,
            )

        # Preserve the pending state between second-factor attempts; otherwise
        # drop back to unverified.
        if keep_candidates:
            session.apply_verification(
                VerificationDecision(
                    state=VerificationState.PENDING_SECOND_FACTOR,
                    candidate_refs=session.candidate_refs,
                    rationale=rationale,
                )
            )
        else:
            session.apply_verification(
                VerificationDecision(state=VerificationState.UNVERIFIED, rationale=rationale)
            )

        return VerificationResult(
            outcome=outcome,
            required_factor=(SecondFactorType.PHONE_LAST_FOUR if keep_candidates else None),
            attempts_remaining=self._remaining(session),
        )

    def _remaining(self, session: SessionState) -> int:
        return max(0, self.max_attempts - session.failed_attempts)
