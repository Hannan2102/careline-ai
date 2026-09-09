"""Medication retrieval.

The most safety-sensitive service in the system. Its job is to return what a
clinician recorded -- never to interpret, complete, or improve it (SAFETY.md).

``EHRProvider.get_medication_request`` returns ``None`` for both "no such
medication" and "the name matched two prescriptions", which is lossy: those
need different responses. Matching therefore happens here, over the patient's
active list, and returns a typed outcome the workflow can act on.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from app.ehr.base import EHRError, EHRProvider
from app.observability.logging import get_logger
from app.schemas.domain import MedicationSummary
from app.services.base import UpstreamUnavailableError, ValidationError

logger = get_logger(__name__)


class MedicationLookupStatus(StrEnum):
    """Outcomes of looking a medication up by name."""

    FOUND = "FOUND"
    #: The patient has no active prescription matching that name.
    NOT_FOUND = "NOT_FOUND"
    #: Several active prescriptions matched; ask which one is meant.
    AMBIGUOUS = "AMBIGUOUS"
    #: Matched, but the record carries no instruction text. Escalate; never
    #: assemble a dosage from the structured fields.
    NO_DOSAGE_ON_FILE = "NO_DOSAGE_ON_FILE"


class MedicationLookup(BaseModel):
    """The result of a medication lookup."""

    model_config = ConfigDict(frozen=True)

    status: MedicationLookupStatus
    medication: MedicationSummary | None = None
    #: Display names of every match, populated when the result is ambiguous.
    candidates: tuple[str, ...] = ()

    @property
    def is_answerable(self) -> bool:
        """True only when there is a stored instruction to read back."""
        return self.status is MedicationLookupStatus.FOUND


class MedicationService:
    """Active prescriptions. Read-only: this system never writes one."""

    def __init__(self, ehr: EHRProvider) -> None:
        self.ehr = ehr

    async def list_active(self, patient_ref: str) -> list[MedicationSummary]:
        try:
            medications = await self.ehr.get_medications(patient_ref)
        except EHRError as exc:
            raise UpstreamUnavailableError(str(exc)) from exc
        logger.info(
            "medications_listed", patient_ref=patient_ref, medication_count=len(medications)
        )
        return medications

    async def look_up(self, patient_ref: str, medication_name: str) -> MedicationLookup:
        """Find one active prescription by name.

        Ambiguity is reported, never resolved by picking the first match: the
        patient is asked which medication they meant.
        """
        needle = medication_name.strip().lower()
        if not needle:
            raise ValidationError("a medication name is required")

        matches = [
            medication
            for medication in await self.list_active(patient_ref)
            if needle in medication.display_name.lower()
        ]

        if not matches:
            logger.info("medication_lookup", patient_ref=patient_ref, outcome="not_found")
            return MedicationLookup(status=MedicationLookupStatus.NOT_FOUND)

        if len(matches) > 1:
            # An exact display-name match disambiguates a substring collision
            # (e.g. "metformin" against "Metformin 500 mg" and "Metformin ER").
            exact = [m for m in matches if m.display_name.lower() == needle]
            if len(exact) == 1:
                matches = exact
            else:
                logger.info("medication_lookup", patient_ref=patient_ref, outcome="ambiguous")
                return MedicationLookup(
                    status=MedicationLookupStatus.AMBIGUOUS,
                    candidates=tuple(m.display_name for m in matches),
                )

        medication = matches[0]
        if not medication.has_dosage_on_file:
            logger.warning(
                "medication_missing_dosage",
                patient_ref=patient_ref,
                medication_request_id=medication.medication_request_id,
            )
            return MedicationLookup(
                status=MedicationLookupStatus.NO_DOSAGE_ON_FILE, medication=medication
            )

        logger.info("medication_lookup", patient_ref=patient_ref, outcome="found")
        return MedicationLookup(status=MedicationLookupStatus.FOUND, medication=medication)

    @staticmethod
    def describe_dosage(medication: MedicationSummary) -> str:
        """Render the stored instruction for reading aloud.

        The instruction is interpolated as an opaque value and attributed to the
        record. The sentence around it may vary; the instruction itself may not.
        Callers must verify the result still contains the stored text verbatim
        before speaking it -- see ``dosage_is_verbatim``.
        """
        if not medication.has_dosage_on_file:
            raise ValidationError(
                f"medication {medication.medication_request_id!r} has no dosage on file; "
                "escalate rather than describing it"
            )
        return (
            f"Your current prescription on file says {medication.display_name}, "
            f"{medication.dosage_instruction}."
        )

    @staticmethod
    def dosage_is_verbatim(rendered: str, medication: MedicationSummary) -> bool:
        """Whether rendered text still contains the stored instruction exactly.

        The guard applied to any model-worded response before it is spoken. If
        this returns False the templated sentence is used instead: the model
        may choose the wording around a dosage, never the dosage.
        """
        instruction = medication.dosage_instruction
        if not instruction:
            return False
        return instruction in rendered
