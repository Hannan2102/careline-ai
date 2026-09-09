# ADR 006 — Budget enforcement in software, not discipline

**Status:** Accepted · **Date:** 2026-09-08

## Context

A hard $15–$20 budget with three metered vendors and an agent that can loop. Vendor
dashboards report spend hours late — far too slow to prevent an overrun, and useless as a
control.

## Decision

Meter locally. Every provider adapter reports units consumed; the usage service prices them
against a rate table and persists to `provider_usage`. A **budget guard** service is
consulted by the provider factory before any paid call:

- below `WARN_ESTIMATED_PROJECT_COST_USD` → proceed
- at/above warn → proceed, warn, surface on the dashboard
- at/above `MAX_ESTIMATED_PROJECT_COST_USD` → block optional paid calls, substitute
  mock/local providers, keep text mode working

Per-session ceilings (cost, turns, TTS characters, STT minutes) apply independently.
`BUDGET_GUARD_OVERRIDE=true` is a deliberate escape hatch, never used in CI.

## Consequences

**Good** — Overspend is prevented rather than discovered. Cost becomes an observable
system property visible in the dashboard. Degradation is graceful: the system stays usable
at the ceiling instead of failing.

**Costs** — Local estimates drift from real invoices (token counts and audio rounding),
so estimates are labelled as estimates and the rate table is kept in one file. The guard
adds a check before each paid call — negligible against a network round trip.

**Placement** — the guard lives in the provider factory, not at call sites, so a future
provider cannot forget to consult it.
