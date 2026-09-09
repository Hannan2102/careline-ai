# ADR 002 — Custom state machine instead of an agent framework

**Status:** Accepted · **Date:** 2026-09-08

## Context

Patient-access conversations are multi-turn and stateful, but the state space is small and
known: verify, book, manage, look up a medication, request a refill, answer an FAQ,
escalate. The system's value depends on *predictability* — a given input must produce the
same safety decision every time. LangGraph and similar frameworks were considered.

## Decision

Implement the orchestrator as an explicit state machine in plain Python, with each workflow
as its own named-state machine over a typed `SessionState`. Use the LLM only for intent,
entity extraction, reference resolution, and wording.

## Consequences

**Good** — Every transition is testable without a model. Safety runs at a fixed point in
the turn that cannot be routed around. No framework upgrade can change conversational
behaviour. A half-finished booking is a serialisable value. Debugging is reading Python.

**Costs** — We write our own retry, repair, and history-summarisation logic, and we do not
get a framework's visualisation tooling for free. Both are small at this scope; the trace
records serve the visualisation need.

**Revisit if** workflows become genuinely branching and numerous enough that hand-written
transitions stop paying for themselves.

## Alternatives

*LangGraph* — good fit for exploratory agent graphs; here it adds a dependency and an
indirection layer over a state machine we can write in a few hundred lines.
*LangChain throughout* — rejected. Its abstractions would sit between us and precisely the
tool-calling and validation behaviour we most need to control.
*A single LLM loop with tools and no explicit state* — rejected outright: verification
state and safety would then live in the prompt, which is not a security boundary.
