# ADR 008 — The model understands; the rules decide

**Status:** Accepted · **Date:** 2026-09-11

## Context

ADR 002 chose an explicit state machine over an agent framework, and ADR 004 chose
discrete STT → LLM → TTS over a speech-to-speech model. Both left one question open: how
much of a turn does the model get to decide?

The first answer was "the opening sentence, and only when the rules found nothing". That
held for a while and then stopped: the phrase tables grew by one entry per live call, and
every entry was a phrasing somebody had already been failed by. A table of phrases is not
a strategy — it is a queue, and it is always exactly as long as the number of callers who
have already had a bad experience.

The recorded turns made the shape of it obvious. Classification took 300–700 ms on an
opening line and *one millisecond* on every answer to a question the agent itself had
asked, because the model was switched off for those. Nineteen bugs found by reading
transcripts; the phrasings that failed were, almost without exception, one-millisecond
turns.

## Decision

The model classifies the whole conversation. Deterministic Python decides what to do,
reads the record, and composes every word the caller hears.

Four rules make that safe to rely on:

**The rules are the floor, not the ceiling.** They run first, on every turn. The model
may overrule an intent they were unsure of and fill an entity they missed; it may never
overwrite a value they parsed. Dates, spoken digits and ordinals resolved against a list
stay theirs, because they have been hardened against real calls and a model has not.
Every way a model can fail — timeout, rate limit, malformed reply, a key that has expired
— lands back on the rules, and the caller notices nothing.

**It is asked only when it would tell us something.** Mid-conversation that means: only
when the rules did not answer the question the agent asked. When they did, a round trip
buys nothing and costs 400 ms on the turns where a caller most notices delay. When they
did not, the alternative was repeating the question, so latency has stopped competing
with a good answer.

**It never sees the record.** It is told which question was asked, and — for appointment
times only — which times, because an empty slot in a diary is not information about a
patient. Which appointments this caller has, and what is on their prescription, do not
leave the process. That is not a preference; it is what makes the privacy claim in
SAFETY.md true.

**Safety runs earlier.** A clinical question is refused before extraction happens at all,
so the classifier cannot reach one.

The model may also say a request is **out of scope** — understood perfectly, and not
something this line does. That is a judgement a phrase table cannot make, and the reply
to it is an offer of a person rather than a recital of the menu.

## Consequences

**Good** — Paraphrase stops being a bug report. The queue of one-phrase-per-incident
fixes is gone. The safety boundary is unchanged: a model that cannot call a tool cannot
cancel the wrong appointment, and one that never sees a record cannot leak one. Mock mode
still runs the whole test suite offline and deterministically, because the rules alone
are a complete implementation.

**Costs** — A vendor outage degrades comprehension rather than ending the call, which is
the right failure but is a real difference in quality that a caller can feel. Groq's free
tier allows 8,000 tokens a minute, so a burst of calls falls back to the rules; that
looks exactly like the model getting worse. Roughly one extra model call per call now,
against about six per call in total.

**Revisit if** the model is ever asked to *plan* — to compose a multi-step action such as
"cancel Thursday and book Friday" as one unit rather than as a shape the rules match.
That is the point at which the tool-calling question in ADR 002 genuinely reopens.

## Alternatives

*Model decides the control flow, with tools.* Rejected, and it is the same rejection as
ADR 002: verification state and safety would live in a prompt, which is not a security
boundary.

*Send the record so the model can resolve "the one for my sugar".* Rejected. It would
work, and it would mean a prescription list crossing the wire to a vendor on an ordinary
turn. The agent asks instead, and after two attempts offers a person.

*Keep the rules only, and keep extending them.* Rejected on evidence: 46 of the 74
phrasings in `tests/unit/test_phrasebook.py` were misrouted when it was written, and that
file is a snapshot of one afternoon's imagination, not of what callers say.
