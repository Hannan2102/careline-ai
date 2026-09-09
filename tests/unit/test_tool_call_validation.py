"""Tool-call validation and repair (Phase 12, docs/agent-tools.md).

The property under test is narrow and absolute: **invalid model output is
never executed**. Everything else here is about failing in a way the model can
recover from without being told anything it should not know.
"""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import BaseModel, Field

from app.ai.providers.base import ProposedToolCall
from app.ai.tool_calls import (
    MAX_REPAIR_ATTEMPTS,
    RepairBudget,
    ToolCallRejection,
    ToolRegistry,
    ValidatedToolCall,
    repair_messages,
    validate_tool_call,
)

VERIFIED = "Patient/demo-john-smith"


class BookAppointmentArgs(BaseModel):
    patient_id: str
    slot_id: str
    appointment_type: str
    reason: str | None = None


class SearchPatientArgs(BaseModel):
    full_name: str = Field(min_length=2)
    date_of_birth: date


@pytest.fixture
def registry() -> ToolRegistry:
    return ToolRegistry(
        {"book_appointment": BookAppointmentArgs, "search_patient": SearchPatientArgs}
    )


def proposal(name: str, **arguments: object) -> ProposedToolCall:
    return ProposedToolCall(tool_call_id="call-1", name=name, arguments=dict(arguments))


class TestValidation:
    def test_a_well_formed_call_validates_into_a_typed_model(self, registry: ToolRegistry) -> None:
        result = validate_tool_call(
            proposal(
                "book_appointment",
                patient_id=VERIFIED,
                slot_id="slot-patel-20260915-1400",
                appointment_type="FOLLOW_UP",
            ),
            registry,
            verified_patient_ref=VERIFIED,
        )
        assert isinstance(result, ValidatedToolCall)
        assert isinstance(result.arguments, BookAppointmentArgs)
        assert result.arguments.slot_id == "slot-patel-20260915-1400"

    def test_a_missing_required_field_is_rejected_not_defaulted(
        self, registry: ToolRegistry
    ) -> None:
        result = validate_tool_call(
            proposal("book_appointment", patient_id=VERIFIED),
            registry,
            verified_patient_ref=VERIFIED,
        )
        assert isinstance(result, ToolCallRejection)
        assert result.code == "INVALID_ARGUMENTS"
        assert "slot_id" in result.message

    def test_a_wrongly_typed_field_is_rejected(self, registry: ToolRegistry) -> None:
        result = validate_tool_call(
            proposal("search_patient", full_name="John Smith", date_of_birth="not-a-date"),
            registry,
        )
        assert isinstance(result, ToolCallRejection)
        assert result.code == "INVALID_ARGUMENTS"

    def test_an_unknown_tool_cannot_be_called_however_it_is_asked_for(
        self, registry: ToolRegistry
    ) -> None:
        result = validate_tool_call(
            proposal("delete_all_appointments", patient_id=VERIFIED),
            registry,
            verified_patient_ref=VERIFIED,
        )
        assert isinstance(result, ToolCallRejection)
        assert result.code == "UNKNOWN_TOOL"

    def test_the_rejection_lists_what_can_be_called(self, registry: ToolRegistry) -> None:
        result = validate_tool_call(proposal("nope"), registry)
        assert isinstance(result, ToolCallRejection)
        assert "book_appointment" in result.message


class TestPatientScoping:
    def test_a_patient_id_for_someone_else_is_refused(self, registry: ToolRegistry) -> None:
        """The model cannot address another patient by guessing an id (ADR 003)."""
        result = validate_tool_call(
            proposal(
                "book_appointment",
                patient_id="Patient/demo-maria-garcia",
                slot_id="slot-1",
                appointment_type="FOLLOW_UP",
            ),
            registry,
            verified_patient_ref=VERIFIED,
        )
        assert isinstance(result, ToolCallRejection)
        assert result.code == "PATIENT_MISMATCH"

    def test_a_bare_id_is_matched_against_the_session_reference(
        self, registry: ToolRegistry
    ) -> None:
        result = validate_tool_call(
            proposal(
                "book_appointment",
                patient_id="demo-john-smith",
                slot_id="slot-1",
                appointment_type="FOLLOW_UP",
            ),
            registry,
            verified_patient_ref=VERIFIED,
        )
        assert isinstance(result, ValidatedToolCall)

    def test_an_unverified_session_reaches_no_patient(self, registry: ToolRegistry) -> None:
        result = validate_tool_call(
            proposal(
                "book_appointment",
                patient_id=VERIFIED,
                slot_id="slot-1",
                appointment_type="FOLLOW_UP",
            ),
            registry,
            verified_patient_ref=None,
        )
        assert isinstance(result, ToolCallRejection)
        assert result.code == "NOT_VERIFIED"

    def test_the_refusal_does_not_say_whether_the_patient_exists(
        self, registry: ToolRegistry
    ) -> None:
        """Otherwise the rejection is a lookup oracle for anyone who can talk."""
        real = validate_tool_call(
            proposal(
                "book_appointment",
                patient_id="Patient/demo-maria-garcia",
                slot_id="slot-1",
                appointment_type="FOLLOW_UP",
            ),
            registry,
            verified_patient_ref=VERIFIED,
        )
        invented = validate_tool_call(
            proposal(
                "book_appointment",
                patient_id="Patient/does-not-exist",
                slot_id="slot-1",
                appointment_type="FOLLOW_UP",
            ),
            registry,
            verified_patient_ref=VERIFIED,
        )
        assert isinstance(real, ToolCallRejection)
        assert isinstance(invented, ToolCallRejection)
        assert real.message == invented.message

    def test_a_tool_without_a_patient_id_needs_no_verified_patient(
        self, registry: ToolRegistry
    ) -> None:
        result = validate_tool_call(
            proposal("search_patient", full_name="John Smith", date_of_birth="1985-02-15"),
            registry,
            verified_patient_ref=None,
        )
        assert isinstance(result, ValidatedToolCall)


class TestRepair:
    def test_the_repair_message_names_the_problem(self, registry: ToolRegistry) -> None:
        rejection = validate_tool_call(
            proposal("book_appointment", patient_id=VERIFIED),
            registry,
            verified_patient_ref=VERIFIED,
        )
        assert isinstance(rejection, ToolCallRejection)
        message = rejection.as_repair_message()
        assert message.role == "tool"
        assert message.tool_call_id == "call-1"
        assert "INVALID_ARGUMENTS" in message.content

    def test_the_repair_message_does_not_echo_the_rejected_values(
        self, registry: ToolRegistry
    ) -> None:
        """Restating them puts them back where the next attempt can copy them."""
        rejection = validate_tool_call(
            proposal(
                "book_appointment",
                patient_id="Patient/demo-maria-garcia",
                slot_id="slot-1",
                appointment_type="FOLLOW_UP",
            ),
            registry,
            verified_patient_ref=VERIFIED,
        )
        assert isinstance(rejection, ToolCallRejection)
        assert "demo-maria-garcia" not in rejection.as_repair_message().content

    def test_repairs_are_bounded(self) -> None:
        budget = RepairBudget()
        assert budget.may_retry("call-1")
        for _ in range(MAX_REPAIR_ATTEMPTS):
            budget.record_attempt("call-1")
        assert budget.exhausted("call-1")

    def test_one_bad_call_does_not_consume_another_calls_allowance(self) -> None:
        budget = RepairBudget()
        for _ in range(MAX_REPAIR_ATTEMPTS):
            budget.record_attempt("call-1")
        assert budget.exhausted("call-1")
        assert budget.may_retry("call-2")

    def test_repair_messages_are_produced_per_rejection(self, registry: ToolRegistry) -> None:
        rejections = [
            r
            for r in (
                validate_tool_call(proposal("nope"), registry),
                validate_tool_call(proposal("also_nope"), registry),
            )
            if isinstance(r, ToolCallRejection)
        ]
        assert len(repair_messages(rejections)) == 2
