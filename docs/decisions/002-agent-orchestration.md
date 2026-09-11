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

## Re-examined 2026-09-11, with evidence

Nineteen bugs from live calls, against the question "would a framework have prevented
this?". Sixteen were domain or parsing bugs no orchestration library touches — a stale
list of offers resolving a choice, a pronoun read as a number, a truncated JSON reply, an
abbreviated month taken for a surname.

Three were state-machine hygiene, and they are the honest concession: a workflow left in
a finished state and never restarted, a question asked by a workflow that had already
closed itself, and an answer the workflow could not attribute to the question it had just
asked. LangGraph's `interrupt()` models "ask, and resume exactly here" as a primitive,
and a checkpointer makes "where are we" an object rather than a flag. Those three would
have been harder to write.

The decision stands, for three reasons the evidence did not change. The framework's main
argument — that the *model* chooses the control flow — is the thing this system
deliberately does not do (ADR 008). The migration cost is not the code but the 1,254
tests that encode what live calls taught, which would have to re-earn their confidence.
And the latency budget has no room for the patterns that come with it.

What was taken from the exercise instead: the outstanding question is now a value a
workflow returns rather than a flag set beside it (`AwaitedInput`, `Offer`), and
`begin_request` makes re-entry into a finished workflow a declared transition rather than
an implicit one.

## Alternatives

*LangGraph* — good fit for exploratory agent graphs; here it adds a dependency and an
indirection layer over a state machine we can write in a few hundred lines.
*LangChain throughout* — rejected. Its abstractions would sit between us and precisely the
tool-calling and validation behaviour we most need to control.
*A single LLM loop with tools and no explicit state* — rejected outright: verification
state and safety would then live in the prompt, which is not a security boundary.
