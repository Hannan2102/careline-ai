"""Shared identity collection.

Every workflow that touches patient data opens the same way: name and date of
birth, a second factor if the details are ambiguous, and a handover after
repeated failure. Keeping one implementation matters more here than elsewhere
-- divergent copies of a security-relevant path drift, and the wording itself
carries a guarantee (an unknown name and a wrong date of birth must produce
identical replies, in every workflow).
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from app.agents.state import SessionState
from app.schemas.domain import AuditAction
from app.services.audit_service import AuditService
from app.services.verification_service import (
    SecondFactorType,
    VerificationOutcome,
    VerificationService,
)
from app.workflows.base import AwaitedInput

ASK_IDENTITY = "Could I take your full name and date of birth?"

#: Deliberately identical whether the record is unknown or the details are
#: wrong. The difference between those two is itself information (ADR 003).
RETRY_IDENTITY = (
    "I couldn't find a match for those details. Could you give me your full name and "
    "date of birth once more?"
)

ASK_SECOND_FACTOR = (
    "Thanks. Could I also take the last four digits of the phone number we have on file?"
)

RETRY_SECOND_FACTOR = (
    "That didn't match what we have on file. Could you try those last four digits again?"
)

LOCKED_OUT = (
    "I haven't been able to confirm your identity, so I'm passing you to our front desk, "
    "who can help verify who you are."
)


class IdentityOutcome(StrEnum):
    VERIFIED = "VERIFIED"
    NEEDS_IDENTITY = "NEEDS_IDENTITY"
    NEEDS_SECOND_FACTOR = "NEEDS_SECOND_FACTOR"
    LOCKED_OUT = "LOCKED_OUT"


class IdentityResult(BaseModel):
    """What the workflow should do next about identity."""

    model_config = ConfigDict(frozen=True)

    outcome: IdentityOutcome
    message: str
    awaiting: AwaitedInput | None = None
    escalation_id: str | None = None

    @property
    def is_verified(self) -> bool:
        return self.outcome is IdentityOutcome.VERIFIED


class IdentityCollector:
    """Runs the identity steps and audits them."""

    def __init__(self, verification: VerificationService, audit: AuditService) -> None:
        self.verification = verification
        self.audit = audit

    @staticmethod
    def ask() -> IdentityResult:
        return IdentityResult(
            outcome=IdentityOutcome.NEEDS_IDENTITY,
            message=ASK_IDENTITY,
            awaiting=AwaitedInput.IDENTITY,
        )

    @staticmethod
    def ask_second_factor() -> IdentityResult:
        return IdentityResult(
            outcome=IdentityOutcome.NEEDS_SECOND_FACTOR,
            message="Could I take the last four digits of the phone number on file?",
            awaiting=AwaitedInput.SECOND_FACTOR,
        )

    async def submit_identity(
        self, session: SessionState, full_name: str | None, date_of_birth: date | None
    ) -> IdentityResult:
        if full_name is None or date_of_birth is None:
            return self.ask()

        self.audit.record(AuditAction.VERIFICATION_ATTEMPTED, session_id=session.session_id)
        result = await self.verification.verify_identity(session, full_name, date_of_birth)

        if result.outcome is VerificationOutcome.VERIFIED:
            return self._verified(session)

        if result.outcome is VerificationOutcome.SECOND_FACTOR_REQUIRED:
            return IdentityResult(
                outcome=IdentityOutcome.NEEDS_SECOND_FACTOR,
                message=ASK_SECOND_FACTOR,
                awaiting=AwaitedInput.SECOND_FACTOR,
            )

        self._record_failure(session)
        if result.outcome in (
            VerificationOutcome.LOCKED_OUT,
            VerificationOutcome.ALREADY_LOCKED,
        ):
            return IdentityResult(
                outcome=IdentityOutcome.LOCKED_OUT,
                message=LOCKED_OUT,
                escalation_id=result.escalation_id,
            )

        return IdentityResult(
            outcome=IdentityOutcome.NEEDS_IDENTITY,
            message=RETRY_IDENTITY,
            awaiting=AwaitedInput.IDENTITY,
        )

    async def submit_second_factor(
        self,
        session: SessionState,
        factor_type: SecondFactorType | None,
        value: str | None,
    ) -> IdentityResult:
        if value is None:
            return self.ask_second_factor()

        result = await self.verification.submit_second_factor(
            session, factor_type or SecondFactorType.PHONE_LAST_FOUR, value
        )

        if result.outcome is VerificationOutcome.VERIFIED:
            return self._verified(session)

        self._record_failure(session)
        if result.outcome in (
            VerificationOutcome.LOCKED_OUT,
            VerificationOutcome.ALREADY_LOCKED,
        ):
            return IdentityResult(
                outcome=IdentityOutcome.LOCKED_OUT,
                message=LOCKED_OUT,
                escalation_id=result.escalation_id,
            )

        return IdentityResult(
            outcome=IdentityOutcome.NEEDS_SECOND_FACTOR,
            message=RETRY_SECOND_FACTOR,
            awaiting=AwaitedInput.SECOND_FACTOR,
        )

    def _verified(self, session: SessionState) -> IdentityResult:
        self.audit.record(
            AuditAction.VERIFICATION_SUCCEEDED,
            session_id=session.session_id,
            patient_ref=session.patient_ref,
        )
        return IdentityResult(outcome=IdentityOutcome.VERIFIED, message="")

    def _record_failure(self, session: SessionState) -> None:
        self.audit.record(
            AuditAction.VERIFICATION_FAILED,
            session_id=session.session_id,
            outcome="denied",
        )
