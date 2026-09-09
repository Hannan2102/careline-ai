# Voice architecture

Phases 13–15. Interfaces exist now; implementations land later.

## Pipeline

```
mic → LiveKit → DeepgramSTTProvider → [turn manager] → orchestrator
    → response text → ElevenLabsTTSProvider → LiveKit → speaker
```

The orchestrator is the same object text mode calls. Voice contributes no business logic.

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

An utterance is not assumed to be a complete command. "I'd like Tuesday" is meaningful only
against the slots just offered — which is why offered slots live in `workflow_state`.

## Streaming discipline

Do not wait for a complete response before speaking. Sentence-boundary chunks go to TTS as
the LLM produces them, so first audio starts while the tail is still generating. Tool calls
are the exception: nothing is spoken about a result until the tool has returned, because
speculative narration of an unfinished EHR write is exactly the failure mode to avoid.

## Telephony (Phase 15)

`Patient phone → Twilio number → SIP trunk → LiveKit room → same voice agent.`
Telephony adds 8 kHz narrowband audio, DTMF, and carrier latency. It changes the STT
configuration and nothing else.

## Voice-specific risks

Names and dates of birth are the hardest recognition problem in this domain and the most
consequential — they gate verification. Mitigations: spell-back confirmation of surnames,
digit-by-digit DOB confirmation, tolerant matching in the verification service (which
compares against candidate records rather than requiring a perfect transcript), and a
bounded retry count before escalating to a human.
