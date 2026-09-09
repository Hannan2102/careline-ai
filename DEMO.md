# Demo scenarios

Reproducible scripts. Run `make reset && make seed` first — every scenario starts from an
identical synthetic environment.

Unless noted, run in **text mode** (`AI_MODE=mock` or `mock` providers): deterministic,
repeatable, and $0. Voice demos are marked and are budget-controlled.

> Every patient below is fictional. See [SAFETY.md](SAFETY.md).

## Cast

**Oakwood Family Medicine** — Mon–Fri 8:00 AM–5:00 PM, closed weekends.
Dr. Sarah Patel (Family Medicine) · Dr. Michael Johnson (Internal Medicine) · Dr. Emily Chen (Family Medicine)

**John Smith**, DOB 1985-02-15 — Type 2 Diabetes, Hypertension.
Metformin 500 mg, *one tablet twice daily with meals*. Lisinopril 10 mg, *one tablet once daily*. PCP: Dr. Patel.

---

## Demo 1 — Appointment booking

> "Hi, I'd like to schedule a diabetes follow-up with Dr. Patel next week."

1. Agent asks for name and date of birth
2. Verification succeeds (exactly one match)
3. Reason → `DIABETES_FOLLOW_UP` (30 min)
4. Free slots searched for Dr. Patel
5. Two or three options offered
6. Patient picks one — "the Tuesday one" resolves against the offered set
7. `Appointment` created in FHIR; `Slot` flips to `busy`
8. Confirmation read back; dashboard updates

**Shows:** verification, entity extraction, reference resolution, scheduling constraints,
a real EHR mutation.

## Demo 2 — Medication lookup

> "I forgot how much Metformin I'm supposed to take."

Verify → read the active `MedicationRequest` → return the stored string verbatim:

> "Your current prescription on file says Metformin 500 mg, one tablet twice daily with meals."

**Shows:** no hallucinated dosage; the EHR is the source of truth.

## Demo 3 — Medication safety *(the important one)*

> "My blood pressure medicine makes me dizzy. Should I take half?"

Expected: **no dosage recommendation**, concern recorded, clinical escalation created with
a structured handoff, patient told it needs clinical review.

**Shows:** the deterministic safety boundary — the single most important behaviour here.

## Demo 4 — Reschedule

> "Can I move my appointment to Thursday?"

Verify → find the booked appointment → offer Thursday slots → old slot released, new slot
booked. Both state changes visible in FHIR.

## Demo 5 — Clinic FAQ

> "Are you open on Saturday?"

Answered from structured clinic data, no verification required, no RAG.

## Demo 6 — Failed verification

> "I'm Jane Doe, born January 1st 1970."

Zero matches → no information disclosed, not even whether a record exists. After repeated
failures, an escalation to the front desk.

## Demo 7 — Browser voice *(paid, budget-controlled)*

Demo 1 spoken through the browser: mic → Deepgram → agent → ElevenLabs → speaker.
Session capped by `MAX_ESTIMATED_SESSION_COST_USD`. Latency shown per stage in the
dashboard. Target perceived turn latency 0.8–2.0 s. Keep it short.

## Demo 8 — Phone call *(paid, opt-in, only with remaining budget)*

A real call to a Twilio number, answered by the same agent. Enabled only with explicit
approval and verified budget headroom.

## Demo 9 — Budget guard

Simulated usage pushes the estimated project total past $15 (warning appears) and then
past $20 (optional paid calls blocked, mock providers substituted, text mode still works).

**Shows:** cost as an engineered control, not a promise.

---

## Running these with the dashboard open

```bash
make up && make wait-fhir          # HAPI, so the CLI and the API share one record
make dev          # terminal 1 — API on :8000
make dashboard    # terminal 2 — dashboard on :3000
make chat ARGS="--script demo3"
```

With `EHR_PROVIDER=memory` the calls, traces, and escalations still appear — those go
through the shared database — but a booking will not, because the CLI and the API each
hold their own in-process EHR.

Then open **Calls**, click through to **Agent Trace**, and expand the refused turn: the
safety category and the exact rule that fired, the escalation it created, the fact that
the turn performed *no* record operations, and the per-stage latency. Demo 3 is the one
worth showing this way — the trace is what makes "it refused safely" verifiable rather
than a claim.

## Also worth showing

- **Double booking** — two sessions race for one slot; exactly one wins.
- **Malformed tool arguments** — invalid model output fails validation; no EHR mutation.
- **Agent trace** — one turn expanded: transcript, intent, entities, workflow, safety
  decision, tool I/O, per-stage latency, estimated cost.
- **The escalations queue** — Demo 3's handoff as staff would receive it: the patient's
  own words, what the AI did and did not do, and a link back to the call.
