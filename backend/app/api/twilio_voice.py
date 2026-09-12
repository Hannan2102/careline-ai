"""The webhook Twilio calls when the phone rings.

Two lines of TwiML: connect the call to a websocket, and let the transport in
``twilio_ws.py`` do the rest. Everything interesting happens there; what
happens here is deciding whether to answer at all.

**The signature check is the point of this module.** This endpoint is
reachable by anyone who finds the URL, and answering a forged request starts a
real call: a recogniser, a model and a synthesiser, all metered, for as long
as the caller stays on the line. Twilio signs every request with the account's
auth token (``X-Twilio-Signature``), and the signature covers the URL as well
as the body -- so it cannot be replayed against a different endpoint.

Validation is implemented here rather than taken from the Twilio SDK for the
same reason every other vendor is reached over ``httpx``: one dependency, one
algorithm, and no version of it hiding in a library this project does not
otherwise need (docs/provider-abstraction.md). It is thirty lines of HMAC and
the format is documented and stable.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Mapping
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from fastapi import APIRouter, Request, Response

from app.config.clinic import CLINIC_NAME
from app.config.settings import Settings, get_settings
from app.observability.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(tags=["voice"])

#: Said only when the line is configured but switched off. A caller should
#: never hear a dead connection, and a caller should never hear a stack trace.
UNAVAILABLE = (
    f"Sorry, the {CLINIC_NAME} automated line is not available right now. "
    "Please call back during opening hours."
)


@router.post("/twilio/voice")
async def incoming_call(request: Request) -> Response:
    """Answer a call by connecting it to the media stream."""
    settings = get_settings()
    form = _fields(await request.body())

    if not _signature_is_valid(request, form, settings):
        # 403 with no detail. A forged request gets told nothing it could use
        # to forge a better one.
        logger.warning("twilio_signature_rejected", path=request.url.path)
        return Response(status_code=403)

    call_sid = str(form.get("CallSid", ""))
    if settings.text_only_mode or not (settings.stt_enabled and settings.tts_enabled):
        logger.warning("twilio_call_refused_voice_disabled", call_sid=call_sid)
        return _twiml(f"<Say>{UNAVAILABLE}</Say><Hangup/>")

    logger.info("twilio_call_answered", call_sid=call_sid, from_number=_masked(form.get("From")))
    stream_url = f"{_public_wss(settings)}/twilio/media"
    # <Connect>, not <Start>: <Start> forks a copy of the audio to a listener,
    # which is for transcription. <Connect> hands the call over, which is the
    # only shape that can send audio back.
    return _twiml(f'<Connect><Stream url="{stream_url}" /></Connect>')


def _fields(body: bytes) -> dict[str, str]:
    """Twilio's POST body, parsed without ``python-multipart``.

    A voice webhook is always ``application/x-www-form-urlencoded``, which the
    standard library reads in one line. Calling ``request.form()`` instead
    would add a dependency to the whole service for a content type it can
    already parse — and this is also the exact set of fields the signature is
    computed over, so decoding it here keeps the two in one place.
    """
    return dict(parse_qsl(body.decode("utf-8", errors="replace"), keep_blank_values=True))


def _twiml(body: str) -> Response:
    return Response(
        content=f'<?xml version="1.0" encoding="UTF-8"?><Response>{body}</Response>',
        media_type="application/xml",
    )


def _public_wss(settings: Settings) -> str:
    """The externally reachable base, as a websocket scheme.

    Twilio dials this from the internet, so it cannot be derived from the
    request's own host header behind a proxy that rewrites it. Configured
    explicitly, and wrong loudly rather than quietly.
    """
    base = (settings.public_base_url or "").rstrip("/")
    if not base:
        raise RuntimeError(
            "PUBLIC_BASE_URL must be set for Twilio: it is the address Twilio "
            "dials back on, and cannot be guessed from the request."
        )
    parts = urlsplit(base)
    return urlunsplit(("wss" if parts.scheme == "https" else "ws", parts.netloc, "", "", ""))


def _signature_is_valid(request: Request, form: Mapping[str, object], settings: Settings) -> bool:
    """Twilio's HMAC-SHA1 over the URL and the sorted POST fields.

    Refuses when no auth token is configured, rather than skipping the check.
    An unsigned endpoint that answers calls is not a degraded mode, it is an
    open one -- and the failure would be invisible until the bill arrived.
    """
    token = settings.twilio_auth_token
    if not token:
        logger.error("twilio_auth_token_missing")
        return False

    supplied = request.headers.get("X-Twilio-Signature", "")
    if not supplied:
        return False

    url = f"{(settings.public_base_url or '').rstrip('/')}{request.url.path}"
    if request.url.query:
        url = f"{url}?{request.url.query}"
    payload = url + "".join(f"{key}{form[key]}" for key in sorted(form))
    expected = base64.b64encode(
        hmac.new(token.encode(), payload.encode("utf-8"), hashlib.sha1).digest()
    ).decode()
    # Constant time: a comparison that returns early leaks how much of a guess
    # was right, one byte at a time.
    return hmac.compare_digest(expected, supplied)


def _masked(number: object) -> str:
    """A caller's number is personal data. The last two digits are enough to
    match a log line to a call without writing one down."""
    text = str(number or "")
    return f"...{text[-2:]}" if len(text) > 2 else "?"


def signature_for(url: str, form: dict[str, str], token: str) -> str:
    """The signature Twilio would send. Used by the tests, and by nothing else."""
    payload = url + "".join(f"{key}{form[key]}" for key in sorted(form))
    return base64.b64encode(
        hmac.new(token.encode(), payload.encode("utf-8"), hashlib.sha1).digest()
    ).decode()


__all__ = ["router", "signature_for"]
