"""Rewriting a written reply into something worth hearing.

The agent's messages are composed once and used twice: rendered in the
dashboard and read aloud. Those two audiences want different text. "Dr." is
correct on screen and is pronounced "drive" by at least one speech engine --
observed live, on every single appointment offer, 120 of them in the recorded
calls.

Applied only on the way to synthesis. The transcript, the trace and the
dashboard keep the written form, because the written form is right there and
changing it to suit a speech engine would be the tail wagging the dog.

Deliberately a small, explicit table rather than a general abbreviation
expander. A general one has to decide whether "St." is Street or Saint, and
guessing wrong out loud is worse than the abbreviation it replaced. Everything
here is an abbreviation this clinic's own data actually produces; anything else
is left exactly as written.
"""

from __future__ import annotations

import re

#: Written form -> spoken form. Matched whole-word, case-sensitively, so a
#: patient surnamed Drive or a street called Oh is untouched.
SPOKEN_FORMS: tuple[tuple[str, str], ...] = (
    # Said as "drive" by Deepgram Aura. The single most common token in the
    # agent's speech, because every offered slot names a clinician.
    (r"\bDr\.", "Doctor"),
    (r"\bDrs\.", "Doctors"),
    (r"\bMr\.", "Mister"),
    (r"\bMrs\.", "Missus"),
    # The clinic's state, in the address read back after booking. Two capital
    # letters before a ZIP is a state code, not a word, and "OH" read as an
    # exclamation is a strange thing to hear in an address.
    (r"\bOH(?=\s+\d{5}\b)", "Ohio"),
)

_COMPILED: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern), replacement) for pattern, replacement in SPOKEN_FORMS
)


def for_speech(text: str) -> str:
    """The same message, written the way it should be said."""
    spoken = text
    for pattern, replacement in _COMPILED:
        spoken = pattern.sub(replacement, spoken)
    return spoken
