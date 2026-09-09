"""Patient verification and the PHI gate (Phase 3, ADR 003).

Runs against both EHR providers. The disclosure assertions matter as much as
the success ones: what the system refuses to reveal is the point.
"""

from __future__ import annotations

from datetime import date

import pytest
from tests.conftest import JOHN_SMITH, JOHN_SMITH_DOB

from app.agents.state import SessionState
from app.ehr.base import EHRProvider
from app.schemas.domain import EscalationCategory, VerificationState
from app.services.access_control import is_phi_accessible, require_verified_patient
from app.services.base import NotVerifiedError, ValidationError
from app.services.escalation_service import EscalationService
from app.services.medication_service import MedicationService
from app.services.patient_service import PatientService
from app.services.scheduling_service import SchedulingService
from app.services.verification_service import (
    SecondFactorType,
    VerificationOutcome,
    VerificationService,
)

AMBIGUOUS_NAME = "Robert Johnson"
AMBIGUOUS_DOB = date(1990, 6, 21)
#: demo-robert-johnson-a / -b, distinguished only by phone and postal code.
ROBERT_A_PHONE_LAST_FOUR = "0411"
ROBERT_A_POSTAL = "45042"
ROBERT_B_POSTAL = "45067"


@pytest.fixture
def escalations() -> EscalationService:
    return EscalationService()


@pytest.fixture
def verification(ehr: EHRProvider, escalations: EscalationService) -> VerificationService:
    return VerificationService(PatientService(ehr), escalations)


@pytest.fixture
def session() -> SessionState:
    return SessionState("sess-test")


class TestSingleMatch:
    async def test_exact_details_verify_the_session(
        self, verification: VerificationService, session: SessionState
    ) -> None:
        result = await verification.verify_identity(session, "John Smith", JOHN_SMITH_DOB)

        assert result.outcome is VerificationOutcome.VERIFIED
        assert result.patient is not None
        assert result.patient.reference == JOHN_SMITH
        assert session.is_verified is True
        assert session.patient_ref == JOHN_SMITH

    async def test_verification_tolerates_spacing_and_case(
        self, verification: VerificationService, session: SessionState
    ) -> None:
        result = await verification.verify_identity(session, "  john   SMITH ", JOHN_SMITH_DOB)
        assert result.is_verified is True


class TestNoMatch:
    async def test_unknown_patient_is_not_verified(
        self, verification: VerificationService, session: SessionState
    ) -> None:
        result = await verification.verify_identity(session, "Jane Doe", date(1970, 1, 1))
        assert result.outcome is VerificationOutcome.NO_MATCH
        assert session.is_verified is False

    async def test_a_wrong_date_of_birth_is_indistinguishable_from_an_unknown_name(
        self, verification: VerificationService, ehr: EHRProvider
    ) -> None:
        """ "That patient exists, but the DOB is wrong" is itself a disclosure."""
        escalations = EscalationService()
        service = VerificationService(PatientService(ehr), escalations)

        wrong_dob = await service.verify_identity(
            SessionState("s1"), "John Smith", date(1985, 2, 16)
        )
        unknown = await service.verify_identity(SessionState("s2"), "Jane Doe", date(1970, 1, 1))

        assert wrong_dob.outcome is unknown.outcome is VerificationOutcome.NO_MATCH
        assert wrong_dob.model_dump() == unknown.model_dump()

    async def test_malformed_details_reveal_nothing_extra(
        self, verification: VerificationService, session: SessionState
    ) -> None:
        """A single name is a failed attempt, not a validation lecture."""
        result = await verification.verify_identity(session, "John", JOHN_SMITH_DOB)
        assert result.outcome is VerificationOutcome.NO_MATCH

    async def test_no_patient_data_is_returned_on_failure(
        self, verification: VerificationService, session: SessionState
    ) -> None:
        result = await verification.verify_identity(session, "Jane Doe", date(1970, 1, 1))
        assert result.patient is None
        assert "Patient/" not in result.model_dump_json()


class TestAmbiguousMatch:
    async def test_duplicate_details_request_a_second_factor(
        self, verification: VerificationService, session: SessionState
    ) -> None:
        result = await verification.verify_identity(session, AMBIGUOUS_NAME, AMBIGUOUS_DOB)

        assert result.outcome is VerificationOutcome.SECOND_FACTOR_REQUIRED
        assert result.required_factor is SecondFactorType.PHONE_LAST_FOUR
        assert session.verification is VerificationState.PENDING_SECOND_FACTOR
        assert session.is_verified is False

    async def test_the_candidates_are_never_revealed(
        self, verification: VerificationService, session: SessionState
    ) -> None:
        """Even the number of matches is withheld."""
        result = await verification.verify_identity(session, AMBIGUOUS_NAME, AMBIGUOUS_DOB)
        payload = result.model_dump_json()
        assert "Patient/" not in payload
        assert "demo-robert" not in payload
        # Held server-side for the second-factor check, not handed out.
        assert len(session.candidate_refs) == 2

    async def test_a_correct_second_factor_verifies_the_right_record(
        self, verification: VerificationService, session: SessionState
    ) -> None:
        await verification.verify_identity(session, AMBIGUOUS_NAME, AMBIGUOUS_DOB)
        result = await verification.submit_second_factor(
            session, SecondFactorType.PHONE_LAST_FOUR, ROBERT_A_PHONE_LAST_FOUR
        )

        assert result.outcome is VerificationOutcome.VERIFIED
        assert result.patient is not None
        assert result.patient.phone_last_four == ROBERT_A_PHONE_LAST_FOUR
        assert session.patient_ref == result.patient.reference

    async def test_postal_code_works_as_an_alternate_factor(
        self, verification: VerificationService, session: SessionState
    ) -> None:
        await verification.verify_identity(session, AMBIGUOUS_NAME, AMBIGUOUS_DOB)
        result = await verification.submit_second_factor(
            session, SecondFactorType.POSTAL_CODE, ROBERT_B_POSTAL
        )
        assert result.outcome is VerificationOutcome.VERIFIED
        assert result.patient is not None
        assert result.patient.postal_code == ROBERT_B_POSTAL

    async def test_phone_factor_ignores_formatting_but_not_value(
        self, verification: VerificationService, session: SessionState
    ) -> None:
        await verification.verify_identity(session, AMBIGUOUS_NAME, AMBIGUOUS_DOB)
        spoken = await verification.submit_second_factor(
            session, SecondFactorType.PHONE_LAST_FOUR, " 0 4 1 1 "
        )
        assert spoken.outcome is VerificationOutcome.VERIFIED

    async def test_a_wrong_second_factor_does_not_verify(
        self, verification: VerificationService, session: SessionState
    ) -> None:
        await verification.verify_identity(session, AMBIGUOUS_NAME, AMBIGUOUS_DOB)
        result = await verification.submit_second_factor(
            session, SecondFactorType.PHONE_LAST_FOUR, "9999"
        )

        assert result.outcome is VerificationOutcome.SECOND_FACTOR_FAILED
        assert session.is_verified is False
        # Still pending, so the caller may try again within the attempt budget.
        assert session.verification is VerificationState.PENDING_SECOND_FACTOR

    async def test_a_second_factor_matching_several_records_is_refused(
        self, verification: VerificationService, session: SessionState, ehr: EHRProvider
    ) -> None:
        """A shared second factor resolves nothing, so it must not verify anyone.

        Built explicitly: a third patient with the same name, date of birth and
        postal code as an existing one, so the factor genuinely collides.
        """
        await PatientService(ehr).register_new_patient(
            "Robert", "Johnson", AMBIGUOUS_DOB, postal_code=ROBERT_A_POSTAL
        )

        first = await verification.verify_identity(session, AMBIGUOUS_NAME, AMBIGUOUS_DOB)
        assert first.outcome is VerificationOutcome.SECOND_FACTOR_REQUIRED
        assert len(session.candidate_refs) == 3

        result = await verification.submit_second_factor(
            session, SecondFactorType.POSTAL_CODE, ROBERT_A_POSTAL
        )
        assert result.outcome is VerificationOutcome.SECOND_FACTOR_FAILED
        assert result.patient is None
        assert session.is_verified is False

    async def test_a_second_factor_out_of_order_is_rejected(
        self, verification: VerificationService, session: SessionState
    ) -> None:
        with pytest.raises(ValidationError, match="verify identity first"):
            await verification.submit_second_factor(
                session, SecondFactorType.PHONE_LAST_FOUR, "0411"
            )


class TestLockout:
    async def test_repeated_failure_locks_the_session_and_escalates(
        self,
        verification: VerificationService,
        session: SessionState,
        escalations: EscalationService,
    ) -> None:
        for _ in range(2):
            result = await verification.verify_identity(session, "Jane Doe", date(1970, 1, 1))
            assert result.outcome is VerificationOutcome.NO_MATCH

        final = await verification.verify_identity(session, "Jane Doe", date(1970, 1, 1))

        assert final.outcome is VerificationOutcome.LOCKED_OUT
        assert final.escalation_id is not None
        assert session.is_locked_out is True

        created = escalations.store.for_session(session.session_id)
        assert len(created) == 1
        assert created[0].category is EscalationCategory.FAILED_VERIFICATION
        assert created[0].destination == "Front desk"
        assert "No patient information" in created[0].ai_action

    async def test_a_locked_session_stops_accepting_attempts(
        self, verification: VerificationService, session: SessionState
    ) -> None:
        """Correct details after lockout must not open the session."""
        for _ in range(3):
            await verification.verify_identity(session, "Jane Doe", date(1970, 1, 1))

        result = await verification.verify_identity(session, "John Smith", JOHN_SMITH_DOB)
        assert result.outcome is VerificationOutcome.ALREADY_LOCKED
        assert session.is_verified is False

    async def test_attempts_remaining_counts_down(
        self, verification: VerificationService, session: SessionState
    ) -> None:
        first = await verification.verify_identity(session, "Jane Doe", date(1970, 1, 1))
        second = await verification.verify_identity(session, "Jane Doe", date(1970, 1, 1))
        assert first.attempts_remaining == 2
        assert second.attempts_remaining == 1


class TestPhiGate:
    """Every patient-specific read must pass the gate."""

    async def test_unverified_sessions_are_refused(self, session: SessionState) -> None:
        with pytest.raises(NotVerifiedError, match="not verified"):
            require_verified_patient(session)
        assert is_phi_accessible(session) is False

    async def test_a_pending_second_factor_is_not_verified_enough(
        self, verification: VerificationService, session: SessionState
    ) -> None:
        await verification.verify_identity(session, AMBIGUOUS_NAME, AMBIGUOUS_DOB)
        with pytest.raises(NotVerifiedError):
            require_verified_patient(session)

    async def test_a_verified_session_yields_its_patient(
        self, verification: VerificationService, session: SessionState
    ) -> None:
        await verification.verify_identity(session, "John Smith", JOHN_SMITH_DOB)
        assert require_verified_patient(session) == JOHN_SMITH

    async def test_a_mismatched_patient_reference_is_refused(
        self, verification: VerificationService, session: SessionState
    ) -> None:
        """Model-supplied ids must not let one caller reach another's record."""
        await verification.verify_identity(session, "John Smith", JOHN_SMITH_DOB)
        with pytest.raises(NotVerifiedError, match="does not match"):
            require_verified_patient(session, "Patient/demo-maria-garcia")

    async def test_the_gate_returns_the_session_reference_not_the_argument(
        self, verification: VerificationService, session: SessionState
    ) -> None:
        await verification.verify_identity(session, "John Smith", JOHN_SMITH_DOB)
        assert require_verified_patient(session, JOHN_SMITH) == session.patient_ref

    async def test_ending_the_session_closes_the_gate(
        self, verification: VerificationService, session: SessionState
    ) -> None:
        await verification.verify_identity(session, "John Smith", JOHN_SMITH_DOB)
        session.end()
        with pytest.raises(NotVerifiedError):
            require_verified_patient(session)


class TestPhiOperationsThroughTheGate:
    """The gate in front of each PHI-shaped operation.

    Written as a table so that a new patient-specific capability has an obvious
    place to be added. Tools land in Phases 4-8 and each calls the gate.
    """

    async def test_every_phi_operation_refuses_without_verification(
        self, ehr: EHRProvider, session: SessionState
    ) -> None:
        scheduling = SchedulingService(ehr)
        medications = MedicationService(ehr)
        patients = PatientService(ehr)

        async def upcoming() -> object:
            return await scheduling.get_upcoming_appointments(require_verified_patient(session))

        async def list_medications() -> object:
            return await medications.list_active(require_verified_patient(session))

        async def look_up_medication() -> object:
            return await medications.look_up(require_verified_patient(session), "metformin")

        async def read_demographics() -> object:
            return await patients.get_patient(require_verified_patient(session))

        async def cancel() -> object:
            return await scheduling.cancel(require_verified_patient(session), "appt-x")

        for operation in (
            upcoming,
            list_medications,
            look_up_medication,
            read_demographics,
            cancel,
        ):
            with pytest.raises(NotVerifiedError):
                await operation()

    async def test_the_same_operations_work_once_verified(
        self,
        ehr: EHRProvider,
        verification: VerificationService,
        session: SessionState,
    ) -> None:
        await verification.verify_identity(session, "John Smith", JOHN_SMITH_DOB)
        patient_ref = require_verified_patient(session)

        assert await SchedulingService(ehr).get_upcoming_appointments(patient_ref) != []
        assert await MedicationService(ehr).list_active(patient_ref) != []
        assert (await PatientService(ehr).get_patient(patient_ref)).family_name == "Smith"
