"""Medication lookup and refill requests (Phases 6 and 7).

Runs against both EHR providers. Two properties carry the weight here: the
dosage read back is byte-identical to the record, and a refill is a request,
never an authorisation.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from tests.conftest import JOHN_SMITH, JOHN_SMITH_DOB

from app.agents.state import SessionState
from app.ehr.base import EHRProvider
from app.schemas.domain import AuditAction, EscalationCategory, Priority, RefillStatus
from app.services.audit_service import AuditService
from app.services.base import ConflictError, NotVerifiedError, ValidationError
from app.services.escalation_service import EscalationService
from app.services.medication_service import MedicationService
from app.services.patient_service import PatientService
from app.services.refill_service import RefillService
from app.services.verification_service import VerificationService
from app.workflows.base import WorkflowStatus
from app.workflows.medication_lookup import (
    LookupState,
    MedicationLookupInput,
    MedicationLookupWorkflow,
)
from app.workflows.refill_request import (
    RefillRequestInput,
    RefillRequestWorkflow,
    RefillState,
)

NOW = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)

LINDA = "Patient/demo-linda-nguyen"
LINDA_DOB = date(1958, 4, 30)
METFORMIN_INSTRUCTION = "One tablet twice daily with meals"


@pytest.fixture
def escalations() -> EscalationService:
    return EscalationService()


@pytest.fixture
def audit() -> AuditService:
    return AuditService()


@pytest.fixture
def refills() -> RefillService:
    return RefillService()


@pytest.fixture
def lookup(
    ehr: EHRProvider, escalations: EscalationService, audit: AuditService
) -> MedicationLookupWorkflow:
    return MedicationLookupWorkflow(
        verification=VerificationService(PatientService(ehr), escalations),
        medications=MedicationService(ehr),
        escalations=escalations,
        audit=audit,
    )


@pytest.fixture
def refill(
    ehr: EHRProvider,
    escalations: EscalationService,
    audit: AuditService,
    refills: RefillService,
) -> RefillRequestWorkflow:
    return RefillRequestWorkflow(
        verification=VerificationService(PatientService(ehr), escalations),
        medications=MedicationService(ehr),
        refills=refills,
        escalations=escalations,
        audit=audit,
    )


@pytest.fixture
def session() -> SessionState:
    return SessionState("sess-meds")


class TestMedicationLookup:
    async def test_the_stored_instruction_is_read_back_verbatim(
        self, lookup: MedicationLookupWorkflow, session: SessionState
    ) -> None:
        """DEMO.md scenario 2, and the core Phase 6 acceptance criterion."""
        await lookup.start(session)
        result = await lookup.advance(
            session,
            MedicationLookupInput(
                full_name="John Smith",
                date_of_birth=JOHN_SMITH_DOB,
                medication_name="Metformin",
            ),
            now=NOW,
        )

        assert result.status is WorkflowStatus.COMPLETED
        assert METFORMIN_INSTRUCTION in result.message
        assert "Metformin 500 mg" in result.message
        # Attributed to the record, not asserted as our own advice.
        assert "prescription on file" in result.message

    async def test_the_answer_is_not_paraphrased(
        self, lookup: MedicationLookupWorkflow, session: SessionState, ehr: EHRProvider
    ) -> None:
        """Compared against the EHR directly, not against a constant."""
        stored = await MedicationService(ehr).look_up(JOHN_SMITH, "metformin")
        assert stored.medication is not None
        assert stored.medication.dosage_instruction is not None

        await lookup.start(session)
        result = await lookup.advance(
            session,
            MedicationLookupInput(
                full_name="John Smith",
                date_of_birth=JOHN_SMITH_DOB,
                medication_name="Metformin",
            ),
            now=NOW,
        )
        assert stored.medication.dosage_instruction in result.message

    async def test_lookup_requires_verification(
        self, lookup: MedicationLookupWorkflow, session: SessionState
    ) -> None:
        result = await lookup.advance(
            session, MedicationLookupInput(medication_name="Metformin"), now=NOW
        )
        assert result.state == LookupState.COLLECTING_IDENTITY.value
        assert METFORMIN_INSTRUCTION not in result.message
        assert session.is_verified is False

    async def test_an_unknown_medication_is_not_invented(
        self, lookup: MedicationLookupWorkflow, session: SessionState
    ) -> None:
        await lookup.start(session)
        result = await lookup.advance(
            session,
            MedicationLookupInput(
                full_name="John Smith",
                date_of_birth=JOHN_SMITH_DOB,
                medication_name="amoxicillin",
            ),
            now=NOW,
        )
        assert "can't find an active prescription for amoxicillin" in result.message
        # It may say what *is* on file, but never a dosage for what is not.
        assert "twice daily" not in result.message

    async def test_a_missing_instruction_escalates_instead_of_guessing(
        self,
        lookup: MedicationLookupWorkflow,
        session: SessionState,
        escalations: EscalationService,
    ) -> None:
        """The deliberate fixture: an active prescription with no dosage text."""
        await lookup.start(session)
        result = await lookup.advance(
            session,
            MedicationLookupInput(
                full_name="Linda Nguyen",
                date_of_birth=LINDA_DOB,
                medication_name="atorvastatin",
                utterance="how much atorvastatin should I take?",
            ),
            now=NOW,
        )

        assert result.status is WorkflowStatus.ESCALATED
        assert "don't want to guess" in result.message

        created = escalations.store.for_session(session.session_id)
        assert created[0].category is EscalationCategory.CLINICAL
        assert created[0].priority is Priority.CLINICAL
        assert created[0].medication_display == "Atorvastatin 20 mg"
        assert "record incomplete" in created[0].ai_action

    async def test_listing_medications_gives_names_without_dosages(
        self, lookup: MedicationLookupWorkflow, session: SessionState
    ) -> None:
        await lookup.start(session)
        result = await lookup.advance(
            session,
            MedicationLookupInput(
                full_name="John Smith", date_of_birth=JOHN_SMITH_DOB, list_all=True
            ),
            now=NOW,
        )
        assert "Metformin 500 mg" in result.message
        assert "Lisinopril 10 mg" in result.message
        assert METFORMIN_INSTRUCTION not in result.message

    async def test_medication_reads_are_audited(
        self, lookup: MedicationLookupWorkflow, session: SessionState, audit: AuditService
    ) -> None:
        await lookup.start(session)
        await lookup.advance(
            session,
            MedicationLookupInput(
                full_name="John Smith",
                date_of_birth=JOHN_SMITH_DOB,
                medication_name="Metformin",
            ),
            now=NOW,
        )
        event = next(e for e in audit.store.all() if e.action is AuditAction.MEDICATIONS_READ)
        assert event.patient_ref == JOHN_SMITH
        assert METFORMIN_INSTRUCTION not in event.model_dump_json()

    async def test_medications_do_not_leak_between_patients(
        self, lookup: MedicationLookupWorkflow, session: SessionState
    ) -> None:
        await lookup.start(session)
        result = await lookup.advance(
            session,
            MedicationLookupInput(
                full_name="Linda Nguyen",
                date_of_birth=LINDA_DOB,
                medication_name="Metformin",
            ),
            now=NOW,
        )
        assert METFORMIN_INSTRUCTION not in result.message
        assert "can't find an active prescription" in result.message


class TestRefillRequests:
    async def test_a_refill_is_sent_for_review_not_approved(
        self, refill: RefillRequestWorkflow, session: SessionState, refills: RefillService
    ) -> None:
        """DEMO.md scenario, and the core Phase 7 acceptance criterion."""
        await refill.start(session)
        confirming = await refill.advance(
            session,
            RefillRequestInput(
                full_name="John Smith",
                date_of_birth=JOHN_SMITH_DOB,
                medication_name="Metformin",
            ),
            now=NOW,
        )
        assert confirming.state == RefillState.CONFIRMING.value

        result = await refill.advance(session, RefillRequestInput(confirm=True), now=NOW)

        assert result.status is WorkflowStatus.COMPLETED
        assert "for review" in result.message
        assert "once a clinician has looked at it" in result.message
        for forbidden in ("approved", "authorised", "authorized", "ready to collect"):
            assert forbidden not in result.message.lower()

        stored = refills.store.for_patient(JOHN_SMITH)
        assert len(stored) == 1
        assert stored[0].status is RefillStatus.PENDING_REVIEW
        assert stored[0].medication_request_id == "medreq-john-metformin"
        assert stored[0].session_id == session.session_id

    async def test_a_refill_never_becomes_a_prescription(
        self, refill: RefillRequestWorkflow, session: SessionState, ehr: EHRProvider
    ) -> None:
        """The EHR must be untouched: a request is not an order (FHIR.md)."""
        before = await MedicationService(ehr).list_active(JOHN_SMITH)

        await refill.start(session)
        await refill.advance(
            session,
            RefillRequestInput(
                full_name="John Smith",
                date_of_birth=JOHN_SMITH_DOB,
                medication_name="Metformin",
            ),
            now=NOW,
        )
        await refill.advance(session, RefillRequestInput(confirm=True), now=NOW)

        after = await MedicationService(ehr).list_active(JOHN_SMITH)
        assert [m.model_dump() for m in before] == [m.model_dump() for m in after]

    async def test_the_refill_service_has_no_way_to_authorise(self) -> None:
        """Not "we don't call it" -- there is no such method to call."""
        api = {name for name in dir(RefillService) if not name.startswith("_")}
        for forbidden in ("approve", "authorise", "authorize", "fulfil", "fulfill", "dispense"):
            assert not any(forbidden in name for name in api)

    async def test_declining_sends_nothing(
        self, refill: RefillRequestWorkflow, session: SessionState, refills: RefillService
    ) -> None:
        await refill.start(session)
        await refill.advance(
            session,
            RefillRequestInput(
                full_name="John Smith",
                date_of_birth=JOHN_SMITH_DOB,
                medication_name="Metformin",
            ),
            now=NOW,
        )
        result = await refill.advance(session, RefillRequestInput(confirm=False), now=NOW)
        assert "haven't sent anything" in result.message
        assert refills.store.all() == []

    async def test_a_duplicate_request_is_not_sent_twice(
        self, refill: RefillRequestWorkflow, session: SessionState, refills: RefillService
    ) -> None:
        for _ in range(2):
            await refill.start(session)
            await refill.advance(
                session,
                RefillRequestInput(
                    full_name="John Smith",
                    date_of_birth=JOHN_SMITH_DOB,
                    medication_name="Metformin",
                ),
                now=NOW,
            )
            result = await refill.advance(session, RefillRequestInput(confirm=True), now=NOW)

        assert "already a refill request" in result.message
        assert len(refills.store.for_patient(JOHN_SMITH)) == 1

    async def test_no_active_prescription_escalates_without_creating_a_request(
        self,
        refill: RefillRequestWorkflow,
        session: SessionState,
        refills: RefillService,
        escalations: EscalationService,
    ) -> None:
        await refill.start(session)
        result = await refill.advance(
            session,
            RefillRequestInput(
                full_name="John Smith",
                date_of_birth=JOHN_SMITH_DOB,
                medication_name="amoxicillin",
            ),
            now=NOW,
        )

        assert result.status is WorkflowStatus.ESCALATED
        assert refills.store.all() == []
        created = escalations.store.for_session(session.session_id)
        assert created[0].ai_action == "No refill request created"

    async def test_a_prescription_without_dosage_text_can_still_be_refilled(
        self, refill: RefillRequestWorkflow, session: SessionState, refills: RefillService
    ) -> None:
        """The order is active; a missing instruction is for the clinician to fix."""
        await refill.start(session)
        await refill.advance(
            session,
            RefillRequestInput(
                full_name="Linda Nguyen",
                date_of_birth=LINDA_DOB,
                medication_name="atorvastatin",
            ),
            now=NOW,
        )
        result = await refill.advance(session, RefillRequestInput(confirm=True), now=NOW)
        assert result.status is WorkflowStatus.COMPLETED
        assert len(refills.store.for_patient(LINDA)) == 1

    async def test_refills_require_verification(
        self, refill: RefillRequestWorkflow, session: SessionState, refills: RefillService
    ) -> None:
        result = await refill.advance(
            session, RefillRequestInput(medication_name="Metformin"), now=NOW
        )
        assert result.state == RefillState.COLLECTING_IDENTITY.value
        assert refills.store.all() == []

    async def test_refill_requests_are_audited(
        self, refill: RefillRequestWorkflow, session: SessionState, audit: AuditService
    ) -> None:
        await refill.start(session)
        await refill.advance(
            session,
            RefillRequestInput(
                full_name="John Smith",
                date_of_birth=JOHN_SMITH_DOB,
                medication_name="Metformin",
            ),
            now=NOW,
        )
        await refill.advance(session, RefillRequestInput(confirm=True), now=NOW)
        assert AuditAction.REFILL_REQUESTED in audit.store.actions()


class TestRefillService:
    async def test_an_inactive_prescription_cannot_be_refilled(
        self, ehr: EHRProvider, refills: RefillService
    ) -> None:
        medication = (await MedicationService(ehr).list_active(JOHN_SMITH))[0]
        stopped = medication.model_copy(update={"status": "stopped"})
        with pytest.raises(ValidationError, match="not active"):
            refills.request_refill(JOHN_SMITH, stopped)

    async def test_a_prescription_belonging_to_someone_else_is_refused(
        self, ehr: EHRProvider, refills: RefillService
    ) -> None:
        medication = (await MedicationService(ehr).list_active(JOHN_SMITH))[0]
        with pytest.raises(ValidationError, match="does not belong"):
            refills.request_refill(LINDA, medication)

    async def test_every_request_starts_pending_review(
        self, ehr: EHRProvider, refills: RefillService
    ) -> None:
        medication = (await MedicationService(ehr).list_active(JOHN_SMITH))[0]
        request = refills.request_refill(JOHN_SMITH, medication)
        assert request.status is RefillStatus.PENDING_REVIEW
        assert refills.pending_for_patient(JOHN_SMITH) == [request]

    async def test_duplicates_conflict_rather_than_stacking_up(
        self, ehr: EHRProvider, refills: RefillService
    ) -> None:
        medication = (await MedicationService(ehr).list_active(JOHN_SMITH))[0]
        refills.request_refill(JOHN_SMITH, medication)
        with pytest.raises(ConflictError, match="already awaiting review"):
            refills.request_refill(JOHN_SMITH, medication)


class TestVerificationGate:
    async def test_an_unverified_session_cannot_read_medications(
        self, ehr: EHRProvider, session: SessionState
    ) -> None:
        from app.services.access_control import require_verified_patient

        with pytest.raises(NotVerifiedError):
            await MedicationService(ehr).list_active(require_verified_patient(session))
