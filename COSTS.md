# Cost management

**Total initial development budget: $15–$20.** This is a hard target, enforced in
software. It is not a $25 budget.

## Why this is an engineering topic

A voice agent is one of the easiest systems to accidentally spend money on: every second
of audio is metered twice (STT in, TTS out) and every turn carries an LLM call. Treating
cost as a runtime concern — metered, priced, and enforced — is part of the design, not an
afterthought.

## Component budget

| Component | Cost |
|---|---|
| HAPI FHIR | $0 |
| PostgreSQL | $0 |
| Synthea | $0 |
| Docker | $0 |
| FastAPI | $0 |
| Next.js | $0 |
| WebSocket transport (ADR 007) | $0 |
| GitHub | free tier |
| **OpenAI** | **~$7** |
| **Deepgram** (recognition *and* Aura speech) | **$0.51 so far** — against a $200 free credit, no card |
| **Groq** (LLM *and* speech) | **$0** — free tier |
| ~~ElevenLabs~~ | not used — see below |
| Contingency | $0–$5 |
| **Target total** | **~$15–$20** |

## Strategy

1. **Text mode for ~90–95% of development.** No STT, no TTS, and a mock LLM by default.
   Cost per iteration: $0.
2. **Automated tests never call a paid API.** Mock providers are the default in CI and in
   `pytest`. A test that needs a paid call is a design smell.
3. **Local components own the heavy lifting.** EHR, database, scheduling, and safety are
   entirely local and free.
4. **Voice sessions are short and deliberate.** Voice is for validating speech recognition
   of names and dates, turn-taking, barge-in, latency, and voice quality — not for
   re-testing scheduling logic.
5. **Prompt discipline.** Concise system prompts, summarised history rather than full
   replay, a low-cost model suited to structured tool calling, and a hard output-token cap.
6. **Everything is metered.** Usage is recorded per provider and priced, so the budget
   number is measured rather than estimated by feel.
7. **Local providers later drive inference toward $0** (Phase 17) without changing the
   application.

## Metering

Every provider adapter reports its units to the usage service, which prices them and
appends a row:

```sql
CREATE TABLE provider_usage (
    id             UUID PRIMARY KEY,
    provider       TEXT        NOT NULL,   -- openai | deepgram | elevenlabs | mock | ollama ...
    session_id     TEXT        NULL,
    metric         TEXT        NOT NULL,   -- input_tokens | output_tokens | requests
                                           -- | stt_seconds | tts_characters | tts_seconds
    quantity       NUMERIC     NOT NULL,
    estimated_cost NUMERIC(10,6) NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

Logged per turn and per session:

- LLM input tokens, output tokens, request count
- STT audio seconds / minutes
- TTS characters, and audio seconds where the provider reports them
- call duration
- estimated cost per turn, per session, and project-to-date

Prices live in one table in `backend/app/ai/usage.py` so that a rate change is a
one-line edit. Estimates are derived from logged usage; no billing-API integration is
needed for a demo, and none is claimed.

## Budget configuration

```env
MAX_ESTIMATED_PROJECT_COST_USD=20
WARN_ESTIMATED_PROJECT_COST_USD=15
MAX_ESTIMATED_SESSION_COST_USD=1
MAX_LLM_OUTPUT_TOKENS=400
MAX_CONVERSATION_TURNS=20
MAX_TTS_CHARACTERS_PER_SESSION=3000
MAX_STT_MINUTES_PER_SESSION=10
BUDGET_GUARD_OVERRIDE=false
```

## Spend survives a restart

The ledger is persisted to `provider_usage`, and the total is read back into memory at
startup. Without that, the $20 ceiling would mean "spent since this process started",
which is not a budget: a crash-and-restart loop could spend indefinitely while every
individual run looked well within limits.

Per-session caps deliberately ignore the carried-forward total — they are about the call
in progress, not the project's history.

`make budget` reads the persisted ledger, so it reports the same number the guard
enforces.

## Guard behaviour

The guard is consulted **before** any paid provider call.

| Estimated project spend | Behaviour |
|---|---|
| < $15 | proceed normally |
| ≥ $15 (warn) | proceed; emit a warning; surface prominently on the dashboard |
| ≥ $20 (max) | **block optional paid calls**; fall back to mock/local providers; text mode continues to work |

Per-session ceilings (`MAX_ESTIMATED_SESSION_COST_USD`, TTS characters, STT minutes,
conversation turns) apply independently, so one runaway session cannot consume the project
budget.

`BUDGET_GUARD_OVERRIDE=true` exists for a deliberate, supervised final demo. It is never
set in CI, and using it is a conscious act.

The guard is a service consulted by the provider factory — not a decorator sprinkled on
call sites — so a new provider cannot forget to check it.

## Free inference

`LLM_PROVIDER=groq` uses Groq's developer tier, which is **rate-limited rather than
metered**: no card, no per-token charge, and roughly 30 requests per minute. It is in
`FREE_PROVIDERS`, so tokens are still counted and priced at zero — the metering path stays
exercised, and the ledger stays honest about what was consumed.

Two consequences worth stating:

- The budget guard will not block it, because there is nothing to block. What binds is the
  vendor's rate limit, which surfaces as a 429 and degrades to the deterministic path
  rather than ending a call.
- **Moving a Groq account to a paid tier means moving `groq` out of `FREE_PROVIDERS` and
  into `RATES`.** Otherwise the ledger silently under-reports and the $20 ceiling stops
  meaning $20.

### Measured, 2026-09-09

`openai/gpt-oss-20b` with `reasoning_effort=low`, on the actual extraction task:

| Utterance | Latency | Extracted |
|---|---|---|
| "schedule a diabetes follow-up with Dr. Patel" | 496 ms | reason, practitioner |
| "My name is John Smith and I was born 15 February 1985" | 133 ms | name, date of birth |
| "The first one please" | 225 ms | *nothing* — correctly |
| "yeah that works" | 315 ms | *nothing* — correctly |

Inside the 200–800 ms budget for the LLM stage, and it declines to invent. A local
`llama3.2:3b` given the same "The first one please" fabricated a full identity —
`full_name: "John Doe", date_of_birth: "1990-05-15"` — which is the single worst place in
this system to hallucinate, since those fields are the inputs to verification.

`reasoning_effort` is not optional for these models. Unset, a 16-token output cap returned
an **empty** message that had spent all 16 tokens thinking.

At demo volume the LLM is ~8% of a voice turn's cost, and $7 of OpenAI budget buys ~12,500
text turns. Groq's advantage here is latency and not needing a card, not the money.

### The voice stack costs nothing (Phase 13, measured)

Groq's Whisper was evaluated as a free replacement for Deepgram and **rejected on
latency**, which is the outcome that made the rest of the arithmetic work.

| | STT | LLM | TTS | per turn | cash |
|---|---|---|---|---|---|
| all-Groq (batch Whisper) | ~830 ms | 133–496 ms | ~400 ms | 1360–1730 ms | $0.00 |
| **Deepgram + Groq + Orpheus** | **150–400 ms** | **133–496 ms** | **~400 ms** | **680–1300 ms** | **$0.00** |
| Deepgram + Groq + ElevenLabs | 150–400 ms | 133–496 ms | 75–150 ms | 360–1050 ms | $6.00 |

Measured, not quoted: `whisper-large-v3` returned a perfect transcript of a 4.5-second
utterance in **530 ms** and `-turbo` in **810 ms** — but that is *batch* latency, clocked
after the utterance has finished. Deepgram streams while the caller speaks and finalises
150–400 ms after they stop. Whisper additionally pays ~300 ms of silence detection before
it can start. The gap is ~400–600 ms per turn, and it is the single largest latency lever
in the pipeline.

The middle row is what this project uses. Deepgram's **$200 free credit needs no card**
and covers ~41,667 streaming minutes — about 13,900 three-minute calls, which is roughly
13,900 more than a portfolio demo needs. Groq's free tier serves the model *and* the
speech.

ElevenLabs Flash is genuinely ~300 ms faster to first audio. It was not taken because its
free tier grants **no API access and no commercial use**, so using it means the Starter
plan at $6/month — the only cash line in the whole project. For a demo, "we measured it
and chose the free path deliberately" is worth more than 300 ms.

The metered rate for ElevenLabs was corrected at the same time. It had been $0.00003 per
character, which is list pricing from a plan this project does not use; the Starter plan
works out roughly 3× that, and a ledger that under-reports cannot enforce a ceiling. It
is now $0.0001, priced as if the marginal character were metered.

## Live smoke tests

The adapters are tested against faked transports, which proves the adapter's own logic but
not that the vendor agrees with it. One live call per provider closes that gap, and it is
the only way to spend money in this repository:

```bash
make smoke-cloud PROVIDER=groq        # requires GROQ_API_KEY (free tier)
make smoke-cloud PROVIDER=openai      # requires OPENAI_API_KEY
```

It refuses unless the provider is named explicitly, `--confirm-spend` is passed, a key is
configured, and the budget guard allows it. The flag is required even for a free tier: it
means "I intend to contact a vendor", which is the decision worth being deliberate about. Afterwards it prints the metered cost of that
single call and persists the usage, so the next run counts it against the ceiling.

Budgeted at **under $0.25 for the whole exercise** (ROADMAP Phase 12). The prompt is
`"Reply with exactly: ok"` with a 16-token output cap; the TTS phrase is three words.
Deepgram synthesises a known sentence with macOS `say` and streams it in 100 ms chunks —
transcribing silence
proves only that a socket opened, and it would still be billed.

## Practices

- Use text mode unless the thing under test is genuinely speech.
- Mock paid APIs in tests. Always.
- Do not resend conversation history the model does not need.
- Keep system prompts short; they are billed on every single turn.
- Cap output tokens; a receptionist answer does not need 800 tokens.
- Do not synthesise audio for text you are only reading in a log.
- Prefer short, deterministic responses — cheaper *and* better UX for voice.
- Check `make budget` before enabling a paid provider.

## Spend log

Actual spend is recorded in [PROJECT_STATUS.md](PROJECT_STATUS.md) and updated whenever a
paid API is used.

| Date | Provider | Purpose | Est. cost | Project total |
|---|---|---|---|---|
| 2026-09-09 | Groq | Live smoke test, `openai/gpt-oss-20b` (76 in / 17 out) | $0.00 | **$0.00** |
| 2026-09-09 | Groq | Extraction probe, 5 utterances (853 in / 262 out) | $0.00 | **$0.00** |
| 2026-09-09 | Deepgram | Streaming recognition, first live voice call | $0.01 | **$0.01** |
| 2026-09-10 | Deepgram | Aura speech synthesis, after Groq's daily TTS cap was hit mid-call | $0.19 | **$0.20** |
| 2026-09-11 | Deepgram | Recognition and speech across the live-call debugging sessions | $0.31 | **$0.51** |
| 2026-09-11 | Groq | Classification, 37 requests (14,033 in / 3,300 out) | $0.00 | **$0.51** |

**$0.51 spent, all of it Deepgram, all of it against its $200 credit rather than billed.**
Groq's free tier still serves both the model and — as a fallback — speech, which is why
an agent that now calls a model on most turns costs nothing per turn.

The switch to Deepgram Aura for synthesis was not a preference. Groq's free TTS tier is
capped at 3,600 characters a day, and hitting it mid-call fell back to the mock provider,
which is *silence* — the agent appeared to answer and the caller heard nothing. A paid
path that works beats a free one that fails quietly, and the fallback now announces
itself over the socket as a `notice` rather than pretending to speak.

Every figure above is *measured* from the vendor's own response headers, not estimated.
Check anytime with `make budget`.

## A limit that is not about money

Groq's free tier allows **8,000 tokens a minute**, which is roughly sixteen
classifications. A real call is nowhere near it — one classification per turn, turns ten
seconds apart. A test script run end to end without pauses is well over it, and every
call past the limit falls back to the rules, which looks exactly like the model getting
worse rather than being throttled. Leave a minute between scripted calls, and read the
log before concluding anything about quality.
