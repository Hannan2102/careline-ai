"""Insurance cover on file.

Deliberately modest about what it knows. The clinic records which plan a
patient presented; it does not hold eligibility, remaining deductible, or the
price of a particular visit. Those are live questions for the payer, and this
service answers none of them -- it reports what is on the card as the clinic
copied it down, and says when that record has lapsed.

Everything here is PHI and passes the same gate as any other patient read
(ADR 003). Which plans the *clinic* accepts is a different question entirely,
answered from static configuration without verification (clinic_faq).
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from app.ehr.base import EHRError, EHRProvider
from app.observability.logging import get_logger
from app.schemas.domain import Coverage
from app.services.base import UpstreamUnavailableError

logger = get_logger(__name__)


class CoverageStatus(StrEnum):
    """What the record says, once read."""

    ACTIVE = "ACTIVE"
    #: On file, but cancelled or past its end date. Distinct from NONE: the
    #: patient believes they are covered, and being told "nothing on file"
    #: would be both wrong and alarming.
    LAPSED = "LAPSED"
    #: Nothing recorded. Not an error -- a patient who has never given the
    #: clinic a card is an ordinary case.
    NONE = "NONE"


class CoverageResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: CoverageStatus
    coverage: Coverage | None = None
    #: Everything on file, so a caller with a primary and a secondary plan can
    #: be told about both rather than only the first.
    all_coverage: tuple[Coverage, ...] = ()


class CoverageService:
    """Reads insurance cover for a verified patient."""

    def __init__(self, ehr: EHRProvider) -> None:
        self.ehr = ehr

    async def for_patient(self, patient_ref: str, today: date | None = None) -> CoverageResult:
        """What the record says about this patient's cover."""
        moment = today or date.today()
        try:
            records = await self.ehr.get_coverage(patient_ref)
        except EHRError as exc:
            raise UpstreamUnavailableError(f"Could not read coverage: {exc}") from exc

        if not records:
            return CoverageResult(status=CoverageStatus.NONE)

        active = [c for c in records if self._is_current(c, moment)]
        if active:
            return CoverageResult(
                status=CoverageStatus.ACTIVE,
                coverage=active[0],
                all_coverage=tuple(active),
            )
        # On file but not current. The most recently ended one is the one the
        # patient is most likely asking about.
        lapsed = sorted(records, key=lambda c: c.period_end or date.min, reverse=True)
        return CoverageResult(
            status=CoverageStatus.LAPSED,
            coverage=lapsed[0],
            all_coverage=tuple(lapsed),
        )

    @staticmethod
    def _is_current(coverage: Coverage, today: date) -> bool:
        """Both conditions, because either alone is wrong.

        A record can be marked active and have run out; it can also be
        cancelled while its end date is still in the future. Trusting only the
        status flag tells a patient they are covered when they are not.
        """
        if not coverage.is_active:
            return False
        return coverage.period_end is None or coverage.period_end >= today
