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
| LiveKit | free tier |
| GitHub | free tier |
| **OpenAI** | **~$7** |
| **Deepgram** | **~$4** |
| **ElevenLabs** | **~$4** |
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

## Live smoke tests

The adapters are tested against faked transports, which proves the adapter's own logic but
not that the vendor agrees with it. One live call per provider closes that gap, and it is
the only way to spend money in this repository:

```bash
make smoke-cloud PROVIDER=openai      # requires OPENAI_API_KEY
```

It refuses unless the provider is named explicitly, `--confirm-spend` is passed, a key is
configured, and the budget guard allows it. Afterwards it prints the metered cost of that
single call and persists the usage, so the next run counts it against the ceiling.

Budgeted at **under $0.25 for the whole exercise** (ROADMAP Phase 12). The prompt is
`"Reply with exactly: ok"` with a 16-token output cap; the TTS phrase is three words.
Deepgram is reported as skipped rather than faked — a smoke test that transcribes silence
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
| — | — | No paid API calls have been made | $0.00 | **$0.00** |

Phase 12 added the adapters that *can* spend; nothing has been spent yet. The first live
smoke test goes in this table with its measured cost, not an estimate.
