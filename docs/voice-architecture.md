# Voice architecture

Phases 13–15. The turn manager, the voice session, and the browser transport are
**built and tested** (Phase 13), and so is telephony (Phase 15) — both over the same
`VoiceSession`.

## What exists

`app/voice/turn_manager.py` owns conversational timing and nothing else. Time is
injected, never read from the clock, so every rule below is tested without sleeping —
21 tests in a third of a second.

`app/voice/session.py` binds an STT stream and a TTS provider to that manager and
enforces the per-session spending cap while the call is running. It is transport-
agnostic: LiveKit, SIP, or a list of byte chunks all look identical from inside.

The split is deliberate. The manager takes injected time because its rules are what
break subtly; the session owns the real clock because it drives the ticker. Mixing the
two — starting a call in the past and ticking in the present — makes every call exceed
its own time limit on the first tick, which is exactly the bug that showed up when they
were not separated.

## Pipeline

```
mic → AudioWorklet → WebSocket → DeepgramSTTProvider → [turn manager] → orchestrator
    → response text → GroqTTSProvider → WebSocket → Web Audio → speaker
```

The orchestrator is the same object text mode calls. Voice contributes no business logic.

## Transport

A plain WebSocket, not WebRTC (ADR 007). The short version: barge-in needs acoustic echo
cancellation, and AEC comes from the **browser** — `getUserMedia({echoCancellation:
true})` — regardless of what carries the bytes. WebRTC adds jitter buffering and packet
loss concealment, which matter over a lossy network and not between a tab and a server on
the same machine; a hosted SFU would add a round trip to a latency budget measured in
hundreds of milliseconds.

The wire protocol is deliberately small:

| Direction | Payload |
|---|---|
| client → server | binary: 16 kHz mono 16-bit PCM · JSON: `{"type": "hangup"}` |
| server → client | binary: 24 kHz mono 16-bit PCM · JSON: `ready`, `transcript`, `interrupt`, `closed`, `error` |

`interrupt` is the one that is easy to omit and impossible to fake. Cancelling synthesis
server-side stops us *producing* audio — all a unit test can observe — but whatever
already crossed the socket sits in scheduled Web Audio nodes with start times in the
future. Without it the agent keeps talking for a second after being interrupted. The turn
manager fires `on_interrupt` **before** cancelling, so the client drops its queue at the
moment the caller spoke rather than once the server has unwound its own task.

Capture runs in an `AudioWorklet` (`frontend/public/mic-worklet.js`) so a React render
cannot stall it, and the capture `AudioContext` is created at 16 kHz so the browser
resamples the device — there is no hand-written interpolation in this project to get
subtly wrong.

Echo cancellation is requested, not guaranteed. `getSettings().echoCancellation` reports
what was actually applied, and the dashboard warns when it was not: a browser that
silently refuses AEC makes the agent interrupt its own sentences, which looks exactly
like a server bug.

## Speech synthesis

Groq (Canopy Labs Orpheus), measured at **~400 ms to first audio** against ElevenLabs
Flash's ~75–150 ms. Slower, and free on the same key the LLM uses; the ~300 ms sits
inside the per-turn budget and the money it saves is the entire project budget (COSTS.md).

The endpoint serves **WAV only** — `pcm`, `mp3`, `opus`, `flac` and `ogg` are all 400s —
and streams it with an unbounded header (`RIFF\xff\xff\xff\xffWAVE`), because the total
length is not known when the first byte is sent. Anything that trusts the declared frame
count reads it as a 24-hour file. The adapter strips the container itself and yields bare
PCM, as a chunk-walking state machine rather than "drop 44 bytes": a WAV header is not a
fixed size, and a few bytes of offset does not raise — it turns speech into static.

## Turn manager

Owns the conversational timing that text mode does not have.

| Concern | Handling |
|---|---|
| End of utterance | Endpointing from the STT provider, plus a silence timer |
| Barge-in | Caller speech during playback cancels TTS immediately and starts a new turn |
| Interim transcripts | Displayed/logged, never acted on; only finals reach the orchestrator |
| Short confirmations | "yes", "that one", "mhm" resolved against the current workflow state |
| Corrections | "no, the Tuesday one" re-resolves against the offered set |
| Silence | Re-prompt once, then offer a human |
| Timeout | Graceful close with a callback offer |
| Overlap | One in-flight turn per session; late finals are queued, not raced |

Queuing happens while the agent is *thinking*, not while it is speaking: speech during
playback is a barge-in, which cancels playback and frees the turn immediately. Worth
knowing before writing a test for it.

Barge-in is decided on **any** speech, interim included. Waiting for a final would mean
talking over the caller for the length of their first phrase. Speech shorter than two
characters is ignored, so a cough does not cancel a sentence.

When the audio stream ends, anything still buffered is flushed and answered rather than
dropped: a stream that has ended is silence by definition, which is the same signal that
ends any other utterance.

An utterance is not assumed to be a complete command. "I'd like Tuesday" is meaningful only
against the slots just offered — which is why offered slots live in `workflow_state`.

## Streaming discipline

Do not wait for a complete response before speaking. Sentence-boundary chunks go to TTS as
the LLM produces them, so first audio starts while the tail is still generating. Tool calls
are the exception: nothing is spoken about a result until the tool has returned, because
speculative narration of an unfinished EHR write is exactly the failure mode to avoid.

## Telephony (Phase 15)

`Patient phone → Twilio number → Media Streams websocket → same VoiceSession.`

Built, and driving the same `VoiceSession` as the browser. No LiveKit and no SIP
interworking: Twilio's Media Streams hands the call's audio over a plain websocket,
which is the transport this project already speaks (ADR 007). The earlier plan routed it
through a LiveKit room, which would have added a hop to somebody else's datacentre for
audio that arrives at our door either way.

**There is no codec.** The prediction above — "it changes the STT configuration and
nothing else" — turned out to be exactly right, and better than expected. Twilio speaks 8
kHz G.711 mu-law both ways; Deepgram's recogniser accepts `encoding=mulaw&sample_rate=8000`
and Aura emits it (verified live: `audio/mulaw;rate=8000`). Asking both vendors for the
format the carrier already speaks means no resampling, no companding, and nowhere for a
byte-offset error to turn speech into static. `voice/telephony.py` is base64 and JSON, and
has a test asserting it never grows an `audioop` or `numpy` import.

Two things telephony does *better* than the browser:

| | Browser | Twilio |
|---|---|---|
| Barge-in | bespoke `interrupt` event, because sent audio is queued in the page | `clear`, a protocol primitive |
| "Has the caller heard this yet?" | estimated by pacing bytes at the sample rate | `mark` echoed back when it has actually played |

What telephony adds is that anyone can dial it. The webhook is signature-checked with
Twilio's HMAC over the URL *and* the body, and refuses when no auth token is configured
rather than skipping the check — "validate if configured" is an open phone line one
environment variable away. The media socket itself cannot be signed (Twilio does not sign
the stream); it is protected by the per-call stream URL and by the limits every call
already has: ten minutes, twenty turns, and a spending cap.

DTMF arrives as its own event and is parsed but not yet acted on. Nothing in the agent
asks the caller to press a number.

### Making a call without deploying

Twilio dials in from the internet, so it needs a public address. A tunnel gives one:

```
brew install cloudflared
make phone
```

`PUBLIC_BASE_URL` is half of the webhook signature *and* the address Twilio is told to
stream to, and a quick tunnel's address is issued when it opens — so the tunnel has to
come up first and the API has to start knowing it. Getting that order wrong produces a
signature that never validates, which looks identical to a misconfigured auth token.
`scripts/phone_line.py` exists to make the order impossible to get wrong.

## Voice-specific risks

## Voice-specific risks

Names and dates of birth are the hardest recognition problem in this domain and the most
consequential — they gate verification. Mitigations: spell-back confirmation of surnames,
digit-by-digit DOB confirmation, tolerant matching in the verification service (which
compares against candidate records rather than requiring a perfect transcript), and a
bounded retry count before escalating to a human.
