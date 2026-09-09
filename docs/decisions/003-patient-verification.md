# ADR 003 — Session-scoped verification as a server-side gate

**Status:** Accepted · **Date:** 2026-09-08

## Context

Nothing patient-specific may be disclosed to an unverified caller — including whether a
record exists at all. The caller controls the entire text channel, so any state derived
from what they say is attacker-controlled.

## Decision

Verification is a server-side value on `SessionState`, writable **only** by the
verification service. Primary factors are full name + date of birth; a secondary factor
(last four of phone, or postal code) is required when more than one candidate matches.
Exactly one match verifies; zero matches disclose nothing; multiple matches never
enumerate candidates. Verification expires with the session.

Every PHI-shaped tool checks the gate itself, in the tool layer, rather than trusting the
workflow that called it.

## Consequences

**Good** — Prompt injection cannot produce a verified session. The check is enforced at the
last layer before data access, so a new workflow cannot accidentally bypass it. Failure
modes are explicit and testable.

**Costs** — A repeated check in every tool (accepted: defence in depth beats DRY here), and
extra conversational turns for callers with common names.

**Known limitation** — name + DOB is weak authentication, and the fact that it mirrors
common clinic practice does not make it strong. Documented in SAFETY.md as a gap a real
deployment must close.
