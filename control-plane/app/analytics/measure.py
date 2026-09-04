"""Pure per-turn measurement primitives.

Ported verbatim-in-spirit from the practice kit's model-behavior.py so the numbers this
product reports are the SAME numbers as the operator's published analytics report — a customer
can line their own report up against the published one. Every function here is pure: text
in, numbers out, no I/O, no clock. That is what lets the golden fixtures pin them down
without a database (control-plane/tests/test_analytics_measure.py).

Nothing here keeps the text it measures. The caller passes transcript text in; only the
counts come back. Persisting those counts — never the text — is what keeps the analytics
content-free and tenant-safe (design record: "Constraints the ingestion must honour").
"""

from __future__ import annotations

import re
import statistics as st

# A bare human nudge — "continue", "keep going", "why did you stop". The follow-through
# signal: if the human's NEXT message was only this, the model handed control back too
# early and the person had to poke it. Same list as model-behavior.py.
NUDGE = re.compile(
    r"^\s*(continue|keep going|go on|proceed|go ahead|carry on|yes|y|yep|do it|"
    r"please continue|finish|finish it|keep working|don'?t stop|why did you stop|"
    r"resume|next|more|ok|okay|sure|go)\s*[.!]?\s*$",
    re.I,
)

# Phrase-density lexicons. These are the escalation/handback signal: how often the model
# asks permission, flags a limitation, hedges, or self-corrects. Counts per final message;
# the report normalizes to per-1000-characters. Identical to model-behavior.py's LEX so the
# density numbers are comparable to the published page.
_LEX_SRC = {
    "caveat": r"\b(caveat|worth flagging|i should flag|one concern|to be clear|"
    r"for transparency|note that|heads[- ]up|strictly speaking|subtle(ty)?)\b",
    "hedge": r"\b(likely|probably|appears to|seems to|may be|might be|could be|"
    r"i suspect|arguably|roughly|approximately|not certain|unclear|ambiguous)\b",
    "limitation": r"\b(i (did not|didn'?t|have not|haven'?t|can'?t|cannot|couldn'?t)|"
    r"not verified|unverified|out of scope|beyond (the )?scope|deferred|"
    r"blocked|stopped short|not covered|did not run|untested)\b",
    "permission": r"\b(want me to|should i |shall i |let me know|do you want|"
    r"would you like|your call|ready when you|say the word|if you want)\b",
    "selfcorrect": r"\b(correction|to correct|i was wrong|actually,|"
    r"i mis(read|stated|understood)|earlier i said)\b",
}
LEX = {k: re.compile(v, re.I) for k, v in _LEX_SRC.items()}

# Below this, a "final message" is too short to carry prose structure and measuring it just
# adds noise (a one-line "done." would swamp the bullet/header densities). model-behavior.py
# uses the same 200-char floor.
MIN_STYLE_CHARS = 200


def is_nudge(text: str | None) -> bool:
    """True if the human message is a bare nudge and nothing else."""
    return bool(text and NUDGE.match(text))


def style(text: str) -> dict | None:
    """Structural/prose metrics for one final assistant message, or None if too short.

    Faithful port of model-behavior.py:style. Raw counts — the report derives per-1000-char
    densities and cross-model means from these, exactly as the practice kit does, so an
    index member can be swapped and re-checked against the underlying counts.
    """
    n = len(text)
    if n < MIN_STYLE_CHARS:
        return None
    words = text.split()
    sents = [x for x in re.split(r"(?<=[.!?])\s+|\n", text) if len(x.split()) > 2]
    m = {
        "chars": n,
        "wps": round(st.mean([len(x.split()) for x in sents]), 2) if sents else 0,
        "bullet": len(re.findall(r"^\s*[-*] ", text, re.M)),
        "header": len(re.findall(r"^#+ ", text, re.M)),
        "bold": text.count("**") // 2,
        "table": len(re.findall(r"^\|", text, re.M)),
        "tick": text.count("`"),
        "emdash": text.count("—"),
        "digit": sum(c.isdigit() for c in text),
        "uwr": round(len(set(w.lower() for w in words)) / max(1, len(words)), 3),
        "longw": sum(1 for w in words if len(w) > 9),
        "words": len(words),
    }
    for k, rx in LEX.items():
        m[k] = len(rx.findall(text))
    return m
