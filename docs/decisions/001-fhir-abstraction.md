# ADR 001 — EHR access behind a provider interface

**Status:** Accepted · **Date:** 2026-09-08

## Context

The demo EHR is a local HAPI FHIR server. A credible portfolio piece should also show the
path to a real EHR (Epic sandbox), and the system must be developable and testable on a
machine without Docker. Agents and workflows must not know which of these is active.

## Decision

Define `EHRProvider` as the single abstraction for all clinical/scheduling data access.
Implement `LocalFHIRProvider` (HAPI over FHIR REST), `MemoryFHIRProvider` (in-process,
no Docker), and later `EpicFHIRProvider`. Selection is config-driven via `EHR_PROVIDER`.

FHIR resource types stop at the adapter boundary. Everything above sees plain Pydantic
domain models.

## Consequences

**Good** — Epic becomes an adapter, not a refactor. Tests run with no Docker and no
network. Dosage extraction has exactly one auditable code path. Agents cannot reach an
unmapped FHIR field.

**Costs** — Two mapping layers to maintain, and a real risk that the memory provider drifts
from HAPI's behaviour. Mitigation: both providers are exercised by the *same* integration
test bodies, and the seed script produces identical resources for both.

## Alternatives

*Query HAPI directly from services* — less code today, but couples business logic to one
server's search-parameter dialect and makes Epic a rewrite.
*A generic ORM over a home-grown schema* — throws away the interoperability story, which
is a large part of the point.
