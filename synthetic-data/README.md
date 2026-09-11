# Synthetic data

> **Every record here is fictional.** No real patient data is used, stored, or
> transmitted. Names, dates of birth, phone numbers, addresses, and clinical details were
> invented for this demo. See [../SAFETY.md](../SAFETY.md).

Two tiers:

| Tier | Count | Committed? | Purpose |
|---|---|---|---|
| **Curated** | 5 patients | yes (`fhir/curated/`) | Reproducible demos and tests |
| **Bulk** (Synthea) | 50–100 | no — generated | Realistic search and dashboard volume |

## Curated records

`fhir/curated/clinic.json` — Location, three Practitioners, PractitionerRoles, Schedules.
`fhir/curated/patients.json` — Patients, Conditions, AllergyIntolerance, MedicationRequests,
Coverage, one Encounter.

Ids are stable and readable (`Patient/demo-john-smith`), so a demo script or a test can
reference a record directly. The `demo-` prefix makes curated records obvious in logs and
in the dashboard.

### The cast

| Patient | DOB | Notable |
|---|---|---|
| **John Smith** | 1985-02-15 | Type 2 Diabetes, Hypertension. Metformin 500 mg *"One tablet twice daily with meals"*; Lisinopril 10 mg *"One tablet once daily"*. Penicillin allergy. PCP Dr. Patel. The primary demo patient |
| **Maria Garcia** | 1972-11-03 | Asthma, Albuterol inhaler. PCP Dr. Chen |
| **Linda Nguyen** | 1958-04-30 | Atorvastatin **with no dosage instruction text** |
| **Robert Johnson** ×2 | 1990-06-21 | **Two patients, same name and DOB**, different phone numbers |

### The deliberate fixtures

Two records exist specifically to make safety-critical paths testable, and are marked with
a `_comment` in the JSON (stripped at load):

- **Linda Nguyen's Atorvastatin has no `dosageInstruction.text`.** The agent must say it
  cannot find the instruction and offer escalation. Assembling a dosage from the structured
  components would be a clinical act, and is forbidden ([SAFETY.md](../SAFETY.md)).
- **Robert Johnson A's cover is cancelled, and Robert Johnson B has none at all.** Three
  answers to "am I covered", and the agent must keep them apart: covered, on file but
  lapsed, and nothing recorded. Telling someone holding an expired card that we have
  nothing on file is both wrong and alarming.
- **Two Robert Johnsons share a name and date of birth.** Primary verification cannot
  resolve them, forcing the second-factor path. The agent must never disambiguate by
  leaking either record ([ADR 003](../docs/decisions/003-patient-verification.md)).

There is deliberately **no** "Jane Doe, 1970-01-01" — that is the failed-verification demo,
and it only works if the record genuinely does not exist.

## Generated slots

Slots are generated at seed time rather than committed, so demos always have availability
in the near future. Three practitioners × working days × a 15-minute grid (08:00–17:00
America/New_York, less a 12:00–13:00 break) = 32 slots per practitioner per day, starting
tomorrow. A 14-day seed produces ~960 `Slot` resources.

The seed also books one standing appointment — John Smith, Dr. Patel, diabetes follow-up —
so the lookup, cancel, and reschedule demos have something to work with immediately.

## Seeding

```bash
# In-memory provider (no Docker): seeds automatically when the backend starts
make dev

# HAPI FHIR
make up && make wait-fhir
python scripts/seed_data.py --provider local

# Preview without writing
python scripts/seed_data.py --dry-run

# Back to a known state (cancels appointments, frees slots, reseeds)
python scripts/reset_demo.py
```

Seeding uses conditional PUT at known ids, so re-running updates in place rather than
creating duplicates.

## Bulk patients with Synthea *(Phase 1 extension)*

```bash
git clone https://github.com/synthetichealth/synthea && cd synthea
./run_synthea -p 75 -s 20260908 --exporter.fhir.export true Ohio Riverton
```

Output lands in `synthea/output/fhir/` as transaction bundles, which POST straight to
`$FHIR_BASE_URL`. Generated output is **not** committed (`.gitignore`) — it is large, and
regenerating from a fixed seed is more honest than checking in a snapshot.

Synthea patients are realistic but unpredictable, which makes them good for volume and
search, and poor for scripted demos. Hence the two tiers.
