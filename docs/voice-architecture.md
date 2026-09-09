# Voice architecture

Phases 13–15. The turn manager, the voice session, and the browser transport are
**built and tested** (Phase 13). Telephony is not (Phase 15).

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

`Patient phone → Twilio number → SIP trunk → LiveKit room → same voice agent.`

This is where WebRTC becomes load-bearing, for reasons unrelated to the browser path:
SIP interworking, and a genuinely lossy network. Nothing above `VoiceSession` changes.
Telephony adds 8 kHz narrowband audio, DTMF, and carrier latency. It changes the STT
configuration and nothing else.

## Voice-specific risks

Names and dates of birth are the hardest recognition problem in this domain and the most
consequential — they gate verification. Mitigations: spell-back confirmation of surnames,
digit-by-digit DOB confirmation, tolerant matching in the verification service (which
compares against candidate records rather than requiring a perfect transcript), and a
bounded retry count before escalating to a human.
