"""The verification gate.

Every operation that touches patient-specific data passes through
:func:`require_verified_patient`. It is enforced here, at the last point before
data access, rather than trusted to the workflow that called it -- a new
workflow should not be able to reach patient data by forgetting a check
(ADR 003).

Two things are enforced:

1. the session is verified, and
2. the patient being addressed is *the session's* patient.

The second matters because tool arguments are produced by a language model. If
the model emits ``patient_id="Patient/demo-maria-garcia"`` during John Smith's
call -- through confusion or through something the caller said -- the request
is refused rather than served.
"""

from __future__ import annotations

from app.agents.state import SessionState
from app.observability.logging import get_logger
from app.services.base import NotVerifiedError

logger = get_logger(__name__)


def require_verified_patient(session: SessionState, requested_ref: str | None = None) -> str:
    """Return the session's verified patient reference, or refuse.

    Args:
        session: the conversation's server-side state.
        requested_ref: a patient reference supplied by the caller, typically
            from model-generated tool arguments. When given it must match the
            verified patient.

    Returns:
        The verified patient reference. Callers should use this value rather
        than the one they passed in.

    Raises:
        NotVerifiedError: the session is not verified, or the reference names
            a different patient.
    """
    if not session.is_verified or session.patient_ref is None:
        logger.info(
            "phi_access_denied",
            session_id=session.session_id,
            reason="session_not_verified",
            verification=session.verification.value,
        )
        raise NotVerifiedError("this session is not verified")

    if requested_ref is not None and requested_ref != session.patient_ref:
        # Worth a warning: a mismatch is either a model error or an attempt.
        logger.warning(
            "phi_access_denied",
            session_id=session.session_id,
            reason="patient_ref_mismatch",
            session_patient_ref=session.patient_ref,
            requested_patient_ref=requested_ref,
        )
        raise NotVerifiedError("requested patient does not match the verified patient")

    return session.patient_ref


def is_phi_accessible(session: SessionState) -> bool:
    """Whether patient-specific data may be read at all.

    For deciding what to *offer* -- clinic hours need no verification, an
    appointment lookup does. Never a substitute for the gate itself.
    """
    return session.is_verified
