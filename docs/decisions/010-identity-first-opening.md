# ADR 010 — Identity first, spelled back, read back

**Status:** Accepted · **Date:** 2026-10-01

## Context

A live demo found two failures, and neither was visible in the test suite.

**A real patient could not verify.** The caller was on file and gave the right name and
date of birth. The recogniser wrote down a different spelling of the surname. That is
normal: "Smith" and "Smyth" are one sound, and the recogniser picks whichever spelling it
prefers. Verification correctly found no match and asked again. The caller repeated
exactly what they had said, the recogniser wrote it down the same way, and it failed
again. Nothing in the call ever showed the caller what the agent had heard, so they had no
way to correct it. The only symptom they saw was "I couldn't find a match", which reads
as "you got your own details wrong".

**A date was decided without asking.** The date parser ran `dateutil` with
`dayfirst=True`, so "03/04/1990" became 3 April. At a US clinic the caller almost
certainly meant 4 March. Nothing was read back, so a wrong but valid date was submitted,
used up one of three attempts, and failed. During registration the same wrong date would
have been written into a new record.

A third finding was structural. Every workflow collected identity at whatever point it
needed it, so the first identity question came only after the caller had explained what
they wanted. That is the reverse of how a front desk works, and it meant the agent's
opening question ("How can I help you today?") was the one whose answer it could do least
with.

## Decision

**Every call opens by taking the caller's identity.** The greeting keeps the clinic's
name and the automated-assistant disclosure, then asks for a first and last name. Until
the caller is verified, the orchestrator sends every turn that safety allows to the
shared identity steps in `workflows/identity.py`. A request made along the way ("I need
to reschedule, my name is John Smith") is kept and acted on the moment verification
succeeds. The caller is not asked "how can I help?" about something they have already
said.

**The name is spelled back, letter by letter.** First, middle and last names are spelled
separately and labelled, because one unbroken string of letters hides where the break
is. Hyphens, apostrophes and spaces are named. A correction can be a bare "no", which
asks for the last name to be spelled, since that is the part recognisers mangle. It can
be a spelling, a NATO or "as in" spelling, or a re-said name. A free-form correction
such as "Jon without the h" is understood by people and not by parsers, so the agent asks
for the spelling. Letters are read by a deterministic parser (`agents/spelling.py`). The
model may notice that a caller is spelling, but it never decides the letters unseen,
because whatever it supplies is spelled back before anything is checked.

**The date of birth is read back with the month as a word.** This happens even when the
parse was unambiguous. The parser knows which date it heard; only the caller knows
whether that is the date they said. Digits are what made "03/04" ambiguous in the first
place, so the read-back never uses them.

**Ambiguity is asked about, never resolved silently.** The new parser
(`agents/dob_parser.py`) collects every reading of the utterance that is a real date in a
plausible lifetime:

- **one reading:** confident;
- **two readings:** the caller is asked "is that March fourth or April third?";
- **none, or more than two:** the caller is asked again, with a hint at the shape that
  parses best.

Month-first is the default for this US clinic, and a day-first reading is accepted only
when it is the sole valid one.

**Verification itself is unchanged.** A confirmed name and date of birth go to
`IdentityCollector.submit_identity` and from there to `VerificationService`, the same
path as before:

- an unknown record and wrong details still produce one identical sentence;
- the second factor on multiple matches is unchanged;
- the three-attempt lockout is unchanged;
- every audit event is unchanged.

A retry runs the same steps again rather than asking for both facts in one breath.

**Three failed confirmations of either half hand the call to the front desk.** That
covers rejected read-backs and answers that could not be understood, counted per half.
Each strike is audited (`identity.not_confirmed`, by half and count, never by value), and
the handover is an escalation like any other.

**Registration uses the same steps, not a copy.** A caller who says they are new
continues from wherever collection had reached. Details already confirmed this call are
reused, including those from an attempt that then failed to match. ADR 009 still holds:
the caller has to say they are new, and a failed verification never routes into
registration on its own.

**A clinic question can come first, behind a switch.** `IDENTITY_FIRST_ALLOW_FAQ`, off
by default, lets an opening question about hours, the address or parking be answered
before a name is given, because nothing on the record is involved. With the switch off,
the question is held like any other request and answered as soon as the caller is
verified.

## Amendment, 2026-10-02 — the second live test

A second call from a browser microphone changed four things.

**The opening asks whether the caller is already a patient.** The greeting now ends
"Are you an existing patient, or are you new and would like to register?". The answer
picks the path: verification against the record, or registration. Registration takes
the same name and date-of-birth steps, then a phone number. Without the question, a new
patient's first experience was failing a verification they could never pass, then being
told they could register. The question is skipped when the caller has already answered
it:

- giving a name, which only someone expecting to be looked up does;
- asking for something only a patient can have, such as an appointment to cancel;
- saying they are new.

An unclear answer is asked again as a yes-or-no: "have you been a patient with us
before?"

**Registration ends with what the agent can do, not a booking.** Going straight from
the last detail to "what would you like to be seen about?" assumed everyone who registers
wants an appointment this minute. Now:

- If the caller asked for one in the same breath ("I'm new, can I get an appointment?"),
  that request is held and acted on.
- Otherwise registration ends with a short menu.
- The first booking afterwards is still the 45-minute new-patient visit. It asks what
  the visit is for, which used to be asked during registration.

**Spelled letters are separated by commas when spoken.** This was measured by
synthesising candidates with Aura and transcribing them back with word timings. Full
stops made Aura drop letters ("H. A. N. N. A. N" came back as "h a n") and swallow
"A" into a 160 ms "uh", which the caller heard as the letter being skipped. Commas fixed
most of it (20 of 24 complete), but a third call found Aura still running a vowel into
the vowel before it: "I, A, N" in Bilzerian lost its A. Measured over repeated runs, a
short pause ("...") before every A, and before any vowel that follows a vowel, completed
53 of 54. Pausing before every vowel, or on both sides of A, did worse.

**Numbers read out in pieces are waited for and joined.** The caller said "One",
paused, then the other nine digits. That became two turns, neither of which held a
phone number. Now:

- While dictating, the voice layer waits 3 s after a trailing digit or letter.
- If a number still arrives split, the first part is kept and joined to the next.
  This applies to a phone number (10 digits) and the four-digit check.
- A turn that adds no digits is never held, so the number is asked for again rather
  than "go on" for ever.

## Consequences

**Good:**

- A misheard name is now something the caller can see and fix, rather than a failure
  they are blamed for.
- No date is submitted that the caller has not heard and agreed to.
- The opening question is one whose answer the agent can always use.
- There is one way into a verified session, whichever entry point started the call
  (voice, the CLI or the HTTP API). Workflows keep their own identity states only as
  defence in depth for typed input.
- Safety still runs first on every turn. An emergency said at the name prompt gets the
  emergency response, exactly as before.

**Costs:**

- **Turns.** Verifying now takes at least four turns where one breath used to do: name,
  yes, date, yes. On the phone that is roughly ten seconds, spent on the step that has
  been causing failures.
- **A caller with no record business waits.** With the switch off, somebody who only
  wants the opening hours gives a name and date of birth first, and if they are not on
  file they are not verified at all. The switch exists for clinics that would rather
  answer first.
- **Speech-engine behaviour.** A spelled name has to be *heard* as letters. The spoken
  form ("J. O. H. N") is the conventional way to force that. Whether Deepgram Aura honours
  it, and in particular whether "A." and "O." come out as letters, has to be checked by
  listening (`make smoke-voice SMOKE_TTS=deepgram`). The test suite cannot hear it.
- **Identity is asked of everyone.** Even a caller who would only have asked a public
  question is identified first, which is the reason the switch exists.

**Revisit if** recogniser keyword boosting for the patient roster (PROJECT_STATUS, next
tasks) makes names reliable enough that spelling every one back costs more turns than it
saves.

## Alternatives

*Spell back only after a failed match.* Rejected. By then an attempt has been used, and
the caller has been told their details did not match, which they will reasonably take
as their own mistake. It also leaves a correct-sounding but wrongly spelled name able to
match the wrong record when two patients' names differ by one letter.

*Pick the more likely reading of an ambiguous date.* Rejected. This is exactly what
`dayfirst=True` did. Being right most of the time is not good enough for the one value
that, when wrong but valid, silently burns an attempt or is written into a new record.

*Let the model resolve corrections like "Jon without the h".* Rejected. It would usually
work. When it did not, the agent would spell back a name nobody had said with the same
confidence as one they had. Asking for a spelling costs one turn and cannot invent
letters.
