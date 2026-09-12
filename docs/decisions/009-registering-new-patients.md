# ADR 009 — The agent may create a patient record, under three conditions

**Status:** Accepted · **Date:** 2026-09-12

## Context

Every workflow in this system begins by proving the caller is already in the record. That
left one caller with nowhere to go: the person who has never been to the clinic. They ask
to book, are asked for a name and date of birth, fail — because there is nothing to match
— and are handed to the front desk. The system's answer to a new patient was "we can't
help you", delivered as if they had got their own date of birth wrong.

The obvious fix is to let the agent register them, and the obvious objection is that
creating a patient record is a write to the clinical system by an unverified caller. That
objection is right about the facts and wrong about the risk, and the difference is worth
writing down.

## Decision

The agent may create a `Patient` record, and only a `Patient` record, when a caller says
they are new. Three conditions hold it in place.

**A caller must assert it.** A failed verification never routes here. Somebody who has
misremembered their date of birth is a patient, not a new patient, and an agent that
offered registration after a failed attempt would manufacture duplicate records out of
ordinary human error. The identity prompt mentions that registration exists — in the same
sentence for everyone who fails, so it signals nothing about the record — and the caller
has to take it.

**A possible duplicate stops the write.** Name and date of birth are searched before
anything is created. A match hands to the front desk without creating a second record.

**Only four things are asked.** A name, a date of birth, a phone number, and what the
visit is about. No insurance identifier, no social security number — nothing a
receptionist would take only with a photo ID in front of them.

## Why this is not the same risk as every other PHI path

Everywhere else the gate exists because the record contains what the caller did *not*
tell us: their prescriptions, their appointments, their cover. Handing those to the wrong
person is the harm, and verification is what makes that unlikely.

A record created during this call contains nothing but what the caller has just said out
loud. There is no history to disclose. Verifying them against it would be verifying them
against their own sentence — the check would consist of asking whether they are who they
just said they were, and they would pass. So the session is opened on the new record by
`VerificationService.accept_registration`, which is honest about what it is: not a
verification, but a decision that there is nothing behind the gate to protect yet.

The gate is still opened by the verification service and nowhere else, because
`VerificationDecision` is the only key to `SessionState.apply_verification` (ADR 003). A
workflow that could mint its own would turn that guarantee into a convention.

## Consequences

**Good** — The clinic's published process is finally true: "we'll take your details over
the phone". A new patient books a 45-minute first appointment in one call, and the
booking workflow runs it rather than a second copy of slot logic.

**Costs** — Three, and they are real.

*The record is created from unverified assertions.* This is what a clinic does on the
phone, and the confirmation says to bring photo ID: the desk closes the loop a phone call
cannot. Nothing clinical hangs on the record until somebody arrives.

*It is a membership oracle.* A caller who supplies a name and date of birth learns
whether that person is already known, because the duplicate branch says so. This is
documented in SAFETY.md rather than defended: closing it would mean no new patient could
ever register themselves, and it is strictly weaker than the disclosure verification
already makes — knowing a real patient's name and date of birth verifies you *as* them,
which is the larger prize.

*Nothing stops a fictitious registration.* Somebody can invent a name and hold an
appointment slot. A real deployment answers that with confirmation by SMS or email before
the slot is held; here the slot is synthetic and the tradeoff is noted rather than
solved.

## Alternatives

*Collect details and create a request for staff, writing nothing.* The refill precedent
(FHIR.md), and it was the first design. Rejected because the two are not alike: a refill
request awaits a clinical judgement that only a clinician can make, while a registration
awaits nothing but typing. It would also mean the agent could not offer an appointment,
which is the entire reason the caller rang.

*Let a failed verification offer registration.* Rejected. It converts the commonest
recoverable error in the system — a misremembered date — into a duplicate record, and a
duplicate is where a clinician reads no allergies and no medications for a person who has
both.

*Refuse, and transfer every new patient to the front desk.* The behaviour before this
ADR. Defensible, and it makes the agent useless to the one caller most likely to be
forming a first impression of the clinic.
