"""The harness-agnostic turn/session record shapes, and the one place a turn is closed.

Two record kinds, mirroring the practice kit so the metric extractor (item -e32c) and the
published page's metrics.json shape carry over unchanged:

  {"k":"turn",    ...}   one human->assistant turn on the main thread
  {"k":"session", ...}   one session, with subagent fan-out census + coding aggregate

Every surface normalizer builds a `TurnAccumulator`, feeds it the turn's assistant content
in order, and calls `.finalize(next_human)`. Putting the close logic HERE, not in each
normalizer, is deliberate: the practice kit had exactly one close() and every metric
(dispatch, work-after-last-dispatch, cold-stop, prose) was defined by it. Two copies would
drift, and a drifted definition is an incomparable number.

DISPATCH is a harness-agnostic superset of the tool names that delegate work. Claude Code
emits Agent/Workflow/Task; opencode emits a `task` tool part that spawns a child session.
Matching any of them lets "work after last dispatch" mean the same thing on both. The
session-level fan-out (how many child sessions/workflow runs actually ran) is counted
separately, from the child-session census, because opencode subagents are child SESSIONS,
not tool calls (design record: "Dispatch, restated").
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import measure

# Tool names, lowercased, that hand work to a subordinate. Superset across harnesses.
DISPATCH = {"agent", "workflow", "task", "subagent"}

# Sentinel a normalizer passes to finalize() when the human interrupted the turn ("[Request
# interrupted"). Distinct from a real next message and from None (end of session).
INTERRUPTED = "\x00INT"

# ---- code-metric detectors (session-level, across main loop AND subagents) -------------

# Bash commands that RUN TESTS. Broad on purpose — a test that ran is the signal, whatever
# the stack. Anchored to the runner token so `pytest-cov install` (setup, not a run) misses.
TEST_RE = re.compile(
    r"\b(pytest|py\.test|go test|cargo test|npm (run )?test|yarn test|pnpm test|"
    r"jest|vitest|mocha|playwright test|rspec|phpunit|gradle test|mvn test|"
    r"make test|ctest|dotnet test|bats)\b",
    re.I,
)
COMMIT_RE = re.compile(r"\bgit\s+commit\b", re.I)
REVERT_RE = re.compile(r"\bgit\s+(revert|reset\s+--hard|checkout\s+--)\b", re.I)

# Tool names that WRITE CODE, lowercased. opencode: edit/write/patch; aider/CC: Edit/Write.
# Used only as a fallback edit signal; opencode's authoritative edit record is the `patch`
# part, counted directly by the normalizer.
EDIT_TOOLS = {"edit", "write", "patch", "multiedit", "apply_patch"}


@dataclass
class TurnAccumulator:
    """One human->assistant turn, being built. Surface-neutral.

    The normalizer sets the labels, then for each assistant message calls `add_assistant`
    and appends tool names / text as it walks the content in order. `finalize` produces the
    record dict, or None for a turn with no attributable assistant model (dropped, exactly
    as model-behavior.py drops `n_asst == 0`).
    """

    tenant: str
    surface: str
    session_id: str
    principal: str
    ts: str | None = None
    user_chars: int = 0

    model: str | None = None
    effort: str | None = None
    interrupted: bool = False  # the model was aborted mid-turn (opencode finish=aborted)
    n_asst: int = 0
    seq: list[str] = field(default_factory=list)  # tool names, in call order
    text_chars: int = 0
    final: str = ""  # last non-empty assistant text block — what a human actually reads
    out_tok: int = 0
    in_tok: int = 0
    cache_w_tok: int = 0
    cache_r_tok: int = 0

    def add_assistant(self, model: str | None, effort: str | None, usage: dict | None) -> None:
        """Open/continue the assistant side of the turn. First model wins (attribution)."""
        self.n_asst += 1
        if self.model is None and model:
            self.model = model
        if self.effort is None and effort:
            self.effort = effort
        u = usage or {}
        self.out_tok += int(u.get("output") or 0)
        self.in_tok += int(u.get("input") or 0)
        self.cache_w_tok += int(u.get("cache_write") or 0)
        self.cache_r_tok += int(u.get("cache_read") or 0)

    def add_tool(self, name: str) -> None:
        self.seq.append((name or "").lower())

    def add_text(self, text: str) -> None:
        t = text or ""
        if t.strip():
            self.text_chars += len(t)
            self.final = t  # last one wins

    def finalize(self, next_human: str | None) -> dict | None:
        """Close the turn into a record dict, or None if there is nothing to attribute."""
        if not self.model or self.n_asst == 0:
            return None
        seq = self.seq
        interrupted = self.interrupted or (next_human == INTERRUPTED)
        disp_idx = [i for i, x in enumerate(seq) if x in DISPATCH]
        rec = {
            "k": "turn",
            "tenant": self.tenant,
            "surface": self.surface,
            "sess": self.session_id,
            "principal": self.principal,
            "ts": self.ts,
            "model": self.model,
            "effort": self.effort,
            "user_chars": self.user_chars,
            "n_asst": self.n_asst,
            "tools": len(seq),
            # per-tool counts, dropping zeros — same compact shape as model-behavior.py
            "tc": {t: seq.count(t) for t in sorted(set(seq)) if seq.count(t)},
            "dispatch": len(disp_idx),
            # work-after-last-dispatch: tool calls AFTER the final dispatch. 0 = the turn
            # ended the instant the workers reported back (a cold stop). None = no dispatch.
            "post_disp": (len(seq) - disp_idx[-1] - 1) if disp_idx else None,
            "text_chars": self.text_chars,
            "out_tok": self.out_tok,
            "in_tok": self.in_tok,
            "cache_w_tok": self.cache_w_tok,
            "cache_r_tok": self.cache_r_tok,
            "style": measure.style(self.final),
            "ends_q": int(self.final.rstrip().endswith("?")),
            "nudge": int(not interrupted
                         and next_human is not None
                         and next_human != INTERRUPTED
                         and measure.is_nudge(next_human)),
            "interrupted": int(interrupted),
        }
        return rec


@dataclass
class CodeAggregate:
    """Session-level coding counters, summed across the main loop AND every subagent.

    Counting only the main thread understates a model that delegates its typing — the
    practice kit flags that mistake as one that reversed a conclusion. So the normalizer
    folds child-session activity in here too.
    """

    edits: int = 0
    chars_written: int = 0
    edit_failures: int = 0
    tool_errors: int = 0
    tests: int = 0
    commits: int = 0
    reverts: int = 0
    _files: list[str] = field(default_factory=list)  # for rework (repeat edits)

    def note_edit(self, files: list[str], chars: int = 0, failed: bool = False) -> None:
        self.edits += 1
        self.chars_written += max(0, int(chars))
        if failed:
            self.edit_failures += 1
        self._files.extend(files or [])

    def note_bash(self, command: str) -> None:
        if TEST_RE.search(command or ""):
            self.tests += 1
        if COMMIT_RE.search(command or ""):
            self.commits += 1
        if REVERT_RE.search(command or ""):
            self.reverts += 1

    def note_tool_error(self) -> None:
        self.tool_errors += 1

    def as_dict(self) -> dict:
        # rework = edits to a path already edited earlier in the session
        seen: set[str] = set()
        rework = 0
        for f in self._files:
            if f in seen:
                rework += 1
            else:
                seen.add(f)
        return {
            "edits": self.edits,
            "chars_written": self.chars_written,
            "edit_failures": self.edit_failures,
            "tool_errors": self.tool_errors,
            "rework": rework,
            "tests": self.tests,
            "commits": self.commits,
            "reverts": self.reverts,
        }


def session_record(
    *,
    tenant: str,
    surface: str,
    session_id: str,
    principal: str,
    model: str | None,
    n_subagents: int,
    n_workflow_runs: int,
    subagent_output_tokens: int,
    code: CodeAggregate | None,
    cost: dict | None = None,
) -> dict:
    """Assemble one session record. `code=None` for a surface that cannot produce code
    metrics (chat): the field is null, never a misleading zero (design record)."""
    return {
        "k": "session",
        "tenant": tenant,
        "surface": surface,
        "sess": session_id,
        "principal": principal,
        "model": model,
        "n_subagents": n_subagents,
        "n_workflow_runs": n_workflow_runs,
        # agents-per-wave: mean fan-out per dispatch wave. With no workflow structure to
        # group by, the whole session is one wave, so this is just the subagent count.
        "agents_per_wave": (
            round(n_subagents / n_workflow_runs, 3) if n_workflow_runs else n_subagents
        ),
        "subagent_output_tokens": subagent_output_tokens,
        "code": code.as_dict() if code is not None else None,
        # cost is filled by the ledger-join item (-0e90); the normalizer leaves it absent.
        "cost": cost,
    }
