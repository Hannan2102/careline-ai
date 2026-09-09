# Cost management — implementation notes

Policy and budget in [../COSTS.md](../COSTS.md). This is the mechanism.

## Path of a paid call

```
caller → provider factory → budget guard → provider adapter → vendor
                                 │              │
                                 │              └→ usage service → provider_usage
                                 └→ block / warn / allow
```

The guard sits in the factory. A provider cannot be obtained without passing it, so a new
adapter cannot forget to check — the check is not the adapter's responsibility.

## Rate table

One module, `backend/app/ai/usage.py`, holds all pricing:

```python
RATES = {
    ("openai", "input_tokens"):   Decimal("..."),   # per 1M tokens
    ("openai", "output_tokens"):  Decimal("..."),
    ("deepgram", "stt_seconds"):  Decimal("..."),
    ("elevenlabs", "tts_characters"): Decimal("..."),
    ("mock", "*"): Decimal("0"),
}
```

Rates are configuration, not knowledge embedded across the codebase. They are labelled with
their source and date, and are **estimates** — vendor rounding and billing granularity mean
the local figure will not match an invoice exactly. It does not need to; it needs to be
conservative enough to prevent an overrun.

## Guard decisions

```python
class BudgetDecision(BaseModel):
    allowed: bool
    status: Literal["ok", "warn", "blocked"]
    project_total_usd: Decimal
    session_total_usd: Decimal
    reason: str | None
```

Checks applied, in order: project max → project warn → session cost cap → session turn cap
→ TTS character cap → STT minute cap. The first that trips determines the outcome. Every
decision is logged; every `warn` and `blocked` surfaces on the dashboard.

## Fallback on block

Blocking is not failing. When the guard blocks, the factory returns the corresponding mock
provider and the session continues in text mode with a clear notice. The system stays
demonstrable at the ceiling — which is what makes the guard safe to leave switched on.

## Testing

Unit tests drive the ledger to $14.99, $15.00, $19.99, and $20.00 and assert the exact
transition at each boundary; assert per-session caps trip independently of the project
total; assert mocks record zero cost while still exercising the metering path; and assert
that `BUDGET_GUARD_OVERRIDE` is ignored when `APP_ENV` is not `development`.

CI runs with `AI_MODE=mock`. A test that reaches a vendor is a failing test.
