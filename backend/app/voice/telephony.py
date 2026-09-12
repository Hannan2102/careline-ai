"""Twilio Media Streams, as data rather than as a connection.

The wire format is small enough to be worth having in one pure module: a
handful of JSON shapes, base64 in and base64 out. Keeping it here means the
transport in ``api/twilio_ws.py`` is about sockets, and every rule about what
a frame looks like is testable without one.

**There is no codec here, and that is the point.** Twilio speaks 8 kHz G.711
mu-law, and the browser transport speaks 16 kHz PCM in and 24 kHz PCM out --
so the obvious reading is that telephony needs resampling and companding in
Python, on the latency budget, for every frame of every call. It does not.
Deepgram's recogniser accepts ``encoding=mulaw&sample_rate=8000``, and Aura
emits it (verified against the live endpoint, which answered
``audio/mulaw;rate=8000``). Asking both vendors for the format the carrier
already speaks costs nothing and removes the entire problem. What is left is
base64 and JSON.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any

#: What a carrier gives us and takes back: G.711 mu-law, 8 kHz, mono.
ENCODING = "mulaw"
SAMPLE_RATE = 8000
CHANNELS = 1

#: Twilio's own name for the same thing, as it appears in a `start` frame.
TWILIO_MEDIA_FORMAT = "audio/x-mulaw"

#: 20 ms of mu-law at 8 kHz. Twilio sends this much per frame and is happiest
#: receiving the same; larger writes are accepted and simply buffered.
FRAME_BYTES = 160


@dataclass(frozen=True)
class StreamStarted:
    """The `start` frame: the only place the stream and call ids appear."""

    stream_sid: str
    call_sid: str


def parse(message: str) -> tuple[str, Any]:
    """Read one inbound frame into ``(event, payload)``.

    ``payload`` is the decoded audio for a media frame, a
    :class:`StreamStarted` for a start, the mark's name for a mark, the digit
    for a keypress, and ``None`` otherwise. An unparseable frame is an unknown
    event rather than an exception: a transport that dies on a message Twilio
    added last week is worse than one that ignores it.
    """
    try:
        frame = json.loads(message)
    except (TypeError, ValueError):
        return "unknown", None
    if not isinstance(frame, dict):
        return "unknown", None

    event = str(frame.get("event", "unknown"))
    match event:
        case "media":
            payload = frame.get("media", {}).get("payload", "")
            try:
                return "media", base64.b64decode(payload)
            except (ValueError, TypeError):
                return "unknown", None
        case "start":
            start = frame.get("start", {})
            return "start", StreamStarted(
                stream_sid=str(frame.get("streamSid") or start.get("streamSid") or ""),
                call_sid=str(start.get("callSid") or ""),
            )
        case "mark":
            return "mark", str(frame.get("mark", {}).get("name", ""))
        case "dtmf":
            return "dtmf", str(frame.get("dtmf", {}).get("digit", ""))
        case _:
            return event, None


def media_frame(stream_sid: str, audio: bytes) -> str:
    """One chunk of the agent's speech, on its way to the caller."""
    return json.dumps(
        {
            "event": "media",
            "streamSid": stream_sid,
            "media": {"payload": base64.b64encode(audio).decode("ascii")},
        }
    )


def mark_frame(stream_sid: str, name: str) -> str:
    """A bookmark in the outbound audio, echoed back when it has been played.

    This is how the session learns that the caller has actually *heard* the
    reply. Twilio accepts audio far faster than it plays it, so the last write
    returning means nothing -- and a session that believes the agent stopped
    talking fifteen seconds early starts its silence timer over its own voice.
    The browser transport estimates this from the sample count; here the
    carrier says so.
    """
    return json.dumps({"event": "mark", "streamSid": stream_sid, "mark": {"name": name}})


def clear_frame(stream_sid: str) -> str:
    """Throw away audio Twilio has buffered but not yet played: barge-in.

    The browser transport needed a bespoke `interrupt` event for this, because
    cancelling synthesis server-side leaves whatever already crossed the socket
    sitting in the client's queue. Twilio has the primitive built in.
    """
    return json.dumps({"event": "clear", "streamSid": stream_sid})
