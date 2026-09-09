"""Patient lookup and registration.

Sits between the tools and the EHR adapter. Search results are returned as
candidates without interpretation: deciding whether a match count means
verified, ambiguous, or unknown belongs to the verification service (ADR 003),
and mixing those concerns is how a lookup quietly becomes an authorisation.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from app.ehr.base import EHRError, EHRProvider, EHRUnavailableError
from app.observability.logging import get_logger
from app.schemas.domain import Patient
from app.services.base import NotFoundError, UpstreamUnavailableError, ValidationError

logger = get_logger(__name__)

#: Rejects typos like a birth year of 1080 without excluding real patients.
MAX_PLAUSIBLE_AGE_YEARS = 130

#: Loose on purpose: formats vary and a spoken number is transcribed unevenly.
MIN_PHONE_DIGITS = 7


class PatientService:
    """Patient records. Returns domain models only."""

    def __init__(self, ehr: EHRProvider) -> None:
        self.ehr = ehr

    async def find_candidates(self, full_name: str, date_of_birth: date) -> list[Patient]:
        """Every patient matching name and date of birth.

        Returns an empty list for an unknown patient -- never a hint that the
        name exists with a different date of birth.
        """
        name = " ".join(full_name.split())
        if not name or len(name.split()) < 2:
            raise ValidationError("a full name (given and family) is required")
        self._validate_date_of_birth(date_of_birth)

        try:
            candidates = await self.ehr.search_patients(name, date_of_birth)
        except EHRUnavailableError as exc:
            raise UpstreamUnavailableError(str(exc)) from exc
        except EHRError as exc:
            raise UpstreamUnavailableError(str(exc)) from exc

        # Count only -- logging the searched name would put a name in the logs
        # for a caller who has not been verified.
        logger.info("patient_search", match_count=len(candidates))
        return candidates

    async def get_patient(self, patient_ref: str) -> Patient:
        try:
            patient = await self.ehr.get_patient(patient_ref)
        except EHRError as exc:
            raise UpstreamUnavailableError(str(exc)) from exc
        if patient is None:
            raise NotFoundError(f"no patient {patient_ref!r}")
        return patient

    async def register_new_patient(
        self,
        given_name: str,
        family_name: str,
        date_of_birth: date,
        phone: str | None = None,
        email: str | None = None,
        postal_code: str | None = None,
    ) -> Patient:
        """Create a synthetic patient record for new-patient booking."""
        given = given_name.strip()
        family = family_name.strip()
        if not given or not family:
            raise ValidationError("both a given name and a family name are required")
        self._validate_date_of_birth(date_of_birth)

        if phone is not None:
            digits = sum(c.isdigit() for c in phone)
            if digits < MIN_PHONE_DIGITS:
                raise ValidationError(f"phone number looks incomplete: {phone!r}")
        if email is not None and ("@" not in email or email.startswith("@")):
            raise ValidationError(f"email address looks invalid: {email!r}")

        try:
            patient = await self.ehr.create_patient(
                given_name=given,
                family_name=family,
                date_of_birth=date_of_birth,
                phone=phone,
                email=email,
                postal_code=postal_code,
            )
        except EHRError as exc:
            raise UpstreamUnavailableError(str(exc)) from exc

        logger.info("patient_registered", patient_ref=patient.reference)
        return patient

    @staticmethod
    def _validate_date_of_birth(value: date, today: date | None = None) -> None:
        reference = today or datetime.now(UTC).date()
        if value > reference:
            raise ValidationError("date of birth cannot be in the future")
        if value.year < reference.year - MAX_PLAUSIBLE_AGE_YEARS:
            raise ValidationError(f"date of birth is implausible: {value.isoformat()}")
