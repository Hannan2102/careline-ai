"""A phone call, over Twilio Media Streams.

Two halves, tested apart because they fail apart. The webhook decides whether
to answer a call at all, and is the only thing standing between a public URL
and a stranger starting metered work on this account. The transport carries
the audio, and is where telephony's differences from the browser live.

No Twilio account is needed for any of it: the wire format is JSON and base64,
and the signature is HMAC over documented input.
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketState

from app.agents.factory import build_runtime
from app.ai.providers.base import Transcript, VoiceSpec
from app.api.twilio_voice import UNAVAILABLE, signature_for
from app.api.twilio_ws import twilio_media
from app.config.settings import Settings
from app.ehr.base import EHRProvider
from app.main import create_app
from app.voice import telephony


class ScriptedSTT:
    """Yields one final transcript per inbound audio frame."""

    name = "mock"

    def __init__(self, utterances: list[str]) -> None:
        self.utterances = utterances

    async def transcribe_stream(self, audio: AsyncIterator[bytes]) -> AsyncIterator[Transcript]:
        index = 0
        async for _chunk in audio:
            if index >= len(self.utterances):
                return
            yield Transcript(
                text=self.utterances[index], is_final=True, speech_final=True, audio_seconds=1.0
            )
            index += 1


class TelephonyTTS:
    """Emits mu-law-shaped bytes, as Aura does when asked for the format."""

    name = "mock"

    async def synthesize_stream(self, text: str, voice: VoiceSpec) -> AsyncIterator[bytes]:
        yield b"\x7f" * 160


TOKEN = "test-auth-token"
BASE = "https://careline.example.com"
FORM = {"CallSid": "CA123", "From": "+15550190142", "To": "+15550192200"}


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        twilio_auth_token=TOKEN,
        public_base_url=BASE,
        text_only_mode=False,
        stt_enabled=True,
        tts_enabled=True,
    )


@pytest.fixture
def client(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr("app.api.twilio_voice.get_settings", lambda: settings)
    return TestClient(create_app())


def signed(url: str = f"{BASE}/twilio/voice", form: dict[str, str] | None = None) -> dict[str, str]:
    return {"X-Twilio-Signature": signature_for(url, form or FORM, TOKEN)}


class TestAnsweringTheCall:
    def test_it_returns_twiml_connecting_the_stream(self, client: TestClient) -> None:
        response = client.post("/twilio/voice", data=FORM, headers=signed())

        assert response.status_code == 200
        assert "application/xml" in response.headers["content-type"]
        assert '<Connect><Stream url="wss://careline.example.com/twilio/media" />' in response.text

    def test_connect_not_start(self, client: TestClient) -> None:
        """`<Start>` forks a copy of the audio to a listener, which is for
        transcription. Only `<Connect>` can send audio back, which is the
        entire point of an agent."""
        response = client.post("/twilio/voice", data=FORM, headers=signed())

        assert "<Connect>" in response.text
        assert "<Start>" not in response.text

    def test_a_disabled_line_says_so_rather_than_dropping(
        self, client: TestClient, settings: Settings
    ) -> None:
        settings.text_only_mode = True

        response = client.post("/twilio/voice", data=FORM, headers=signed())

        assert UNAVAILABLE in response.text
        assert "<Hangup/>" in response.text


class TestTheSignature:
    """The only thing between a public URL and a stranger spending the budget.

    Answering a forged request starts a real call: a recogniser, a model and a
    synthesiser, all metered, for as long as whoever sent it stays on the line.
    """

    def test_an_unsigned_request_is_refused(self, client: TestClient) -> None:
        response = client.post("/twilio/voice", data=FORM)

        assert response.status_code == 403
        assert "Connect" not in response.text

    def test_a_wrong_signature_is_refused(self, client: TestClient) -> None:
        response = client.post("/twilio/voice", data=FORM, headers={"X-Twilio-Signature": "nope"})

        assert response.status_code == 403

    def test_a_signature_for_different_form_fields_is_refused(self, client: TestClient) -> None:
        """The body is signed, so a replayed signature cannot carry new fields."""
        response = client.post(
            "/twilio/voice", data={**FORM, "From": "+15559999999"}, headers=signed()
        )

        assert response.status_code == 403

    def test_a_signature_for_a_different_url_is_refused(self, client: TestClient) -> None:
        """The URL is signed too, so a valid signature cannot be moved."""
        elsewhere = signed(url="https://someone-else.example.com/twilio/voice")

        assert client.post("/twilio/voice", data=FORM, headers=elsewhere).status_code == 403

    def test_no_configured_token_refuses_rather_than_skips(
        self, client: TestClient, settings: Settings
    ) -> None:
        """An unsigned endpoint answering calls is not a degraded mode.

        The tempting shape is "validate if a token is configured", which is an
        open phone line one missing environment variable away, and invisible
        until the bill.
        """
        settings.twilio_auth_token = None

        assert client.post("/twilio/voice", data=FORM, headers=signed()).status_code == 403

    def test_the_refusal_says_nothing(self, client: TestClient) -> None:
        response = client.post("/twilio/voice", data=FORM, headers={"X-Twilio-Signature": "nope"})

        assert response.text == ""


class TestTheWireFormat:
    def test_a_media_frame_decodes_to_audio(self) -> None:
        frame = json.dumps(
            {"event": "media", "media": {"payload": base64.b64encode(b"\xff\x7f").decode()}}
        )
        assert telephony.parse(frame) == ("media", b"\xff\x7f")

    def test_a_start_frame_carries_the_ids(self) -> None:
        frame = json.dumps(
            {"event": "start", "streamSid": "MZ1", "start": {"callSid": "CA1", "tracks": []}}
        )
        event, payload = telephony.parse(frame)

        assert event == "start"
        assert isinstance(payload, telephony.StreamStarted)
        assert (payload.stream_sid, payload.call_sid) == ("MZ1", "CA1")

    @pytest.mark.parametrize("junk", ["", "not json", "[]", '{"no event": 1}', "{}"])
    def test_an_unreadable_frame_is_ignored_not_fatal(self, junk: str) -> None:
        """Twilio adds events. A transport that dies on an unfamiliar one is
        worse than one that skips it."""
        event, payload = telephony.parse(junk)

        assert event in {"unknown", ""}
        assert payload is None

    def test_undecodable_audio_is_not_passed_on(self) -> None:
        frame = json.dumps({"event": "media", "media": {"payload": "!!!not base64!!!"}})
        assert telephony.parse(frame) == ("unknown", None)

    def test_outbound_audio_is_base64_against_the_stream(self) -> None:
        frame = json.loads(telephony.media_frame("MZ1", b"\x01\x02"))

        assert frame["event"] == "media"
        assert frame["streamSid"] == "MZ1"
        assert base64.b64decode(frame["media"]["payload"]) == b"\x01\x02"

    def test_clear_is_the_barge_in(self) -> None:
        """The browser transport needed a bespoke event for this because audio
        already sent sits in the page's queue. Twilio has the primitive."""
        assert json.loads(telephony.clear_frame("MZ1")) == {"event": "clear", "streamSid": "MZ1"}

    def test_a_mark_is_how_playback_completion_comes_back(self) -> None:
        frame = json.loads(telephony.mark_frame("MZ1", "utterance-2"))
        assert frame == {"event": "mark", "streamSid": "MZ1", "mark": {"name": "utterance-2"}}

        assert telephony.parse(json.dumps(frame)) == ("mark", "utterance-2")


class TestTheFormatIsTheCarriers:
    def test_nothing_here_converts_audio(self) -> None:
        """The whole codec question, answered by asking the vendors instead.

        Deepgram's recogniser accepts mu-law at 8 kHz and Aura emits it
        (verified live: the endpoint answered `audio/mulaw;rate=8000`). So
        telephony adds no resampling, no companding, and no place for a
        byte-offset error to turn speech into static.
        """
        source = (telephony.__file__ or "").replace(".py", ".py")
        with open(source) as handle:
            body = handle.read()

        for absent in ("audioop", "resample", "lin2ulaw", "ulaw2lin", "numpy"):
            assert absent not in body, f"telephony grew a codec: {absent}"

    def test_it_asks_for_what_the_carrier_speaks(self) -> None:
        assert (telephony.ENCODING, telephony.SAMPLE_RATE) == ("mulaw", 8000)


class FakeTwilio:
    """A Twilio Media Streams client, close enough to be worth trusting.

    It speaks the real frames: a `start`, then base64 audio in, and it echoes
    a `mark` back the way Twilio does when it has finished playing. That echo
    is the part worth faking properly — the transport blocks on it, so a
    double that never sent one would make every call look like it hung.
    """

    def __init__(self, utterances: int = 1) -> None:
        self.sent: list[dict] = []
        self.inbound: list[str] = [
            json.dumps({"event": "connected", "protocol": "Call", "version": "1.0.0"}),
            json.dumps(
                {"event": "start", "streamSid": "MZ1", "start": {"callSid": "CA1", "tracks": []}}
            ),
        ]
        for _ in range(utterances):
            self.inbound.append(
                json.dumps(
                    {
                        "event": "media",
                        "streamSid": "MZ1",
                        "media": {"payload": base64.b64encode(b"\xff" * 160).decode()},
                    }
                )
            )
        self.client_state = WebSocketState.CONNECTED
        self._closed = asyncio.Event()

    async def accept(self) -> None:
        return None

    async def receive(self) -> dict:
        await asyncio.sleep(CHUNK_GAP)
        if self.inbound:
            return {"type": "websocket.receive", "text": self.inbound.pop(0)}
        await self._closed.wait()
        return {"type": "websocket.disconnect"}

    async def send_text(self, text: str) -> None:
        frame = json.loads(text)
        self.sent.append(frame)
        # Twilio echoes a mark once it has played everything before it. Doing
        # the same here is what lets `drain` return.
        if frame["event"] == "mark":
            self.inbound.append(text)

    async def close(self, code: int = 1000) -> None:
        self.client_state = WebSocketState.DISCONNECTED
        self._closed.set()

    @property
    def audio(self) -> bytes:
        return b"".join(
            base64.b64decode(f["media"]["payload"]) for f in self.sent if f["event"] == "media"
        )

    @property
    def events(self) -> list[str]:
        return [f["event"] for f in self.sent]


CHUNK_GAP = 0.02


class TestAWholeCallOverTheCarrier:
    @pytest.fixture(autouse=True)
    def _telephony_runtime(
        self, memory_ehr: EHRProvider, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        runtime = build_runtime(ehr=memory_ehr, settings=Settings(_env_file=None, app_env="test"))
        monkeypatch.setattr("app.api.twilio_ws.get_runtime", lambda: runtime)
        monkeypatch.setattr("app.api.twilio_ws.get_settings", lambda: settings)

        def speech(*_args: object, **kwargs: object) -> object:
            return ScriptedSTT(["Are you open on Saturday?"])

        monkeypatch.setattr("app.api.twilio_ws.build_stt_provider", speech)
        monkeypatch.setattr(
            "app.api.twilio_ws.build_tts_provider",
            lambda *a, **k: TelephonyTTS(),
        )

    async def test_the_agent_answers_down_the_phone(self) -> None:
        socket = FakeTwilio(utterances=2)

        await twilio_media(socket)  # type: ignore[arg-type]

        assert socket.audio, "the caller heard nothing"
        assert "media" in socket.events

    async def test_it_waits_to_be_told_the_reply_was_played(self) -> None:
        """A mark after the audio, and the session waits for its echo.

        Twilio accepts audio far faster than it plays it. Without the mark the
        session believes the agent stopped talking seconds early and starts
        the silence timer over its own voice — the bug the browser transport
        had to be taught to avoid by pacing bytes.
        """
        socket = FakeTwilio(utterances=2)

        await twilio_media(socket)  # type: ignore[arg-type]

        assert "mark" in socket.events
        assert socket.events.index("media") < socket.events.index("mark")

    async def test_the_vendors_are_asked_for_the_carriers_format(
        self, memory_ehr: EHRProvider, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No resampling anywhere, because nobody is asked for the wrong thing."""
        asked: list[dict] = []

        def record(*_args: object, **kwargs: object) -> object:
            asked.append(kwargs)
            return ScriptedSTT([])

        monkeypatch.setattr("app.api.twilio_ws.build_stt_provider", record)
        monkeypatch.setattr("app.api.twilio_ws.build_tts_provider", record)

        await twilio_media(FakeTwilio())  # type: ignore[arg-type]

        assert asked, "no provider was built"
        for call in asked:
            assert call["encoding"] == "mulaw"
            assert call["sample_rate"] == 8000
