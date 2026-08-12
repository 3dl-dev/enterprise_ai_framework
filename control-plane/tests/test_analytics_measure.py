"""The prose/escalation measurement primitives — the arithmetic, pinned.

These are the numbers the published page (3dl.dev/coding-vs-orchestration.html) reports, so
a regression here would silently make a customer's report disagree with the reference. The
expected values are computed by hand from the input text, not snapshotted from the code.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.analytics import measure  # noqa: E402


def test_is_nudge_matches_bare_pokes_only():
    for yes in ["continue", "keep going", "why did you stop", "  proceed. ", "yes"]:
        assert measure.is_nudge(yes), yes
    for no in ["continue building the parser", "why did you stop the deploy?", "", None]:
        assert not measure.is_nudge(no), no


def test_style_returns_none_below_floor():
    assert measure.style("too short") is None
    assert measure.style("x" * (measure.MIN_STYLE_CHARS - 1)) is None


def test_style_counts_structure_and_escalation():
    text = (
        "# Heading\n"
        "Here is a plan that runs well past the two hundred character floor so the "
        "measurement actually fires and returns a populated dict for these assertions.\n"
        "- first bullet\n"
        "- second bullet\n"
        "It is **bold** here. Want me to proceed, or should I wait? This part is likely fine.\n"
    )
    m = measure.style(text)
    assert m is not None
    assert m["chars"] == len(text)
    assert m["header"] == 1          # one "# " line
    assert m["bullet"] == 2          # two "- " lines
    assert m["bold"] == 1            # one **...** pair
    assert m["permission"] >= 1      # "Want me to" / "should i"
    assert m["hedge"] >= 1           # "likely"
    assert m["words"] == len(text.split())
    assert 0.0 < m["uwr"] <= 1.0
