"""Live proof of enterpriseaiframework-e6f's DONE condition: the assistant reaches for the
right tool ON ITS OWN, given a plain question that names no tool, no skill, and no
instruction to search or to run code.

WHY THIS NEEDS A REAL MODEL. fakeprovider never invokes a tool — every reply is a
deterministic hash of the prompt, regardless of what tools are attached (see
tests/test_skill_corpus.py's own docstring for the same reasoning) — so it cannot exercise
"the model decides" at all. That decision is exactly what this file measures, which needs
glm-5.2@deepinfra over Forge, real money, real latency.

WHAT THIS DOES NOT TEST. All three tools already exist and are individually provable:
tests-live/test_web_search_grounding.py proves grounded search, tests/test_code_execution.py
proves real sandboxed execution, tests/test_skill_corpus.py proves manual skill invocation.
None of those turns a tool on BY ITSELF — every one of them sets the relevant
`ephemeralAgent` flag AND, for skills, forces a specific name via `manualSkills`. This file
is the one difference from all three: every prompt below is sent with every tool ATTACHED
(`web_search=True, execute_code=True, skills_catalog=True` — the "badge" toggle with no
forced names, i.e. the model's own `skill(name)` tool over the full accessible catalog, per
`resolveAgentScopedSkillIds`'s ephemeral-agent branch) and the prompt itself never names a
tool, a skill, or an instruction to search/compute. What varies is only whether the turn
carries the SYSTEM PROMPT this item added to librechat.yaml's modelSpec `promptPrefix`
telling the model to decide this for itself.

TRAP THIS FILE IS DESIGNED AROUND (item's own warning). "A turn sent without the relevant
flag gets no tool at all and the model answers from weights while looking entirely
reasonable." That is a statement about TOOL ATTACHMENT, not tool SELECTION, and it holds in
BOTH conditions measured here — baseline and with-instructions both attach all three tools.
The only thing baseline lacks is the system-prompt text; an evaluation that instead dropped
the ephemeralAgent flags for "baseline" would be measuring whether tools exist, which this
item is not — six other items already proved that.

THE SYSTEM PROMPT UNDER TEST IS READ OUT OF THE COMMITTED CONFIG, NOT DUPLICATED HERE. Both
`bundle/librechat/librechat.yaml` modelSpecs (`glm-5-2`, `glm-4-7`) carry an identical
`preset.promptPrefix` block written for this item. Parsing it out of the YAML at test time
means the "before" and "after" measured here are the literal text the deployed surface
serves, not a copy that can silently drift from it. It is sent as the turn's own top-level
`promptPrefix` field (`chat_turn.build_payload`'s `prompt_prefix` kwarg) rather than via
`spec`, which exercises the identical server-side field
(`packages/api/src/agents/load.ts#loadEphemeralAgent`: `req.body?.promptPrefix` is read
whenever the turn carries no model-spec-level promptPrefix of its own) without requiring the
turn to resolve a `spec` name against `modelSpecs.list` first — see that kwarg's docstring.

TOOL SELECTION AGAINST A LIVE MODEL IS STOCHASTIC (operational guard 9). Each prompt is run
`RUNS_PER_PROMPT` times per condition, independently. A prompt is scored "selected
correctly" if the MAJORITY of ITS ACTUALLY-RECORDED runs invoked the expected tool category
— that is the pass bar, stated here rather than papered over with a retry loop. The item's
own DONE condition ("at least the clear-cut majority" of ~10 prompts) is asserted only
against the WITH-INSTRUCTIONS condition; the no-instructions baseline is measured and
printed for the before/after comparison the item asks for, and is not itself a pass/fail
gate — there is no prior claim that unprompted tool selection worked at all before this
item, so nothing regresses if baseline is weak.

CRASH-SURVIVABLE AND RESUMABLE (enterpriseaiframework-e6f wave 6 — the sweep was killed
twice by host disk pressure and lost every result both times because pytest buffers stdout
and nothing was written to disk mid-run). Every call's result is appended to
`RESULTS_JSONL` THE MOMENT IT RETURNS — flushed and fsync'd, not batched, not dependent on
an end-of-run summary or on pytest's own buffering. On each invocation, `_run_condition`
reads that file first and skips any (condition, prompt_id, run_index) already recorded, so
re-running this file (even after a SIGTERM mid-sweep) resumes rather than restarts and
re-spends calls. `RESULTS_JSONL` lives under `tests-live/` in the git worktree, not inside
`bundle/` — a torn-down stack (`docker compose down -v`) does not take it with it. Scoring
is a pure pass over that file (`_score`), so a partial file — even one left by a kill signal
mid-write, whose last line may be truncated JSON — still yields a real, honest verdict: run
`python tests-live/test_tool_selection.py` any time, with no live calls and no bundle
required, to print the current score off whatever is on disk right now.

RUNS_PER_PROMPT IS A PARAMETER, not a constant (same wave), defaulting to something that
fits a short window on a shared, disk-constrained host — override with the
`TOOL_SELECTION_RUNS_PER_PROMPT` env var. Because the JSONL accumulates across
invocations, raising this value and re-running only performs the DELTA of new runs still
needed per (condition, prompt); it does not re-spend calls already recorded.

FAILURES ARE RECORDED, NOT HIDDEN. Every per-prompt result (each run's chosen tool
category, or the caught exception) is both appended to the JSONL and printed via a
session-scoped summary at the end of the test, so a failing prompt names which one, and
why, in -v output — this is the "documented failure list" the item's DONE condition
requires as part of done, not an admission of failure.

Run (bundle must be up, with FORGE_API_KEY configured and the gateway catalogue
re-rendered against it — see bundle/bin/render-gateway-config.py):
  .venv-test/bin/pytest tests-live/test_tool_selection.py -v --tb=short -s -p no:cacheprovider

Score only, no live calls, safe at any time including mid-sweep:
  python tests-live/test_tool_selection.py
"""

import json
import os
import time
from pathlib import Path

import pytest
import yaml

import chat_turn
import oidc_login

REPO = Path(__file__).resolve().parent.parent
BUNDLE = REPO / "bundle"
LIBRECHAT_YAML = BUNDLE / "librechat" / "librechat.yaml"

# In the worktree, NOT under bundle/ — `docker compose down -v` must not take this with it,
# and a re-invocation after a killed run must find it right where it left it.
RESULTS_JSONL = REPO / "tests-live" / "tool_selection_results.jsonl"

MODEL = "glm-5.2@deepinfra"
ENDPOINT_NAME = "Enterprise AI"
ENDPOINT_TYPE = "custom"

# Shrink the window: default to a small number of runs per prompt per condition so one
# invocation fits in a short window on a disk-constrained shared host. Accumulate more
# confidence by invoking again — the JSONL means a re-run only performs the calls still
# missing, it never re-spends what's already recorded.
RUNS_PER_PROMPT = int(os.environ.get("TOOL_SELECTION_RUNS_PER_PROMPT", "1"))
# WAS 100.0 -- demonstrably too short for a web_search turn that fetches several pages
# (killed node-lts and hn-top in wave 6, both recorded as timeouts, not wrong-tool
# choices). Raised per this wave's fix; override with TOOL_SELECTION_TURN_TIMEOUT.
TURN_TIMEOUT = float(os.environ.get("TOOL_SELECTION_TURN_TIMEOUT", "220.0"))

# Prompts, deliberately NOT naming a tool, a skill, or an instruction to search/compute.
# Each carries a stable id (used as the JSONL key, so the file survives edits to the
# prompt wording) and is tagged with the tool category a competent unprompted assistant
# reaches for. Two skill prompts is all the bundled tenant fixture corpus (bundle/skills/)
# supports — these are the SAME two skills tests/test_skill_corpus.py already uses as
# fixture content for manual invocation; using them again here to test AUTOMATIC
# invocation is reusing an existing test fixture, not coupling the platform to tenant
# content (guard #9 is about the SYSTEM PROMPT naming a skill, which the promptPrefix text
# does not).
PROMPTS = [
    ("py-version",
     "What is the latest stable release version of the Python programming language?",
     "web_search"),
    ("openai-ceo",
     "Who is the current CEO of OpenAI?",
     "web_search"),
    ("node-lts",
     "What is the newest LTS release line of Node.js right now?",
     "web_search"),
    ("hn-top",
     "What's the top story on Hacker News right now?",
     "web_search"),
    ("multiply",
     "What is 384195 multiplied by 88213, exactly?",
     "execute_code"),
    ("stdev",
     "What is the standard deviation of these numbers: 14, 8, 21, 3, 45, 9, 30, 17, 2, 26?",
     "execute_code"),
    ("sort-median",
     "Sort this list and give me the median: 55, 3, 89, 12, 47, 6, 71, 24",
     "execute_code"),
    ("reverse-string",
     "Reverse the string 'enterpriseaiframeworkdogfood' and tell me the 7th character of "
     "the reversed string.",
     "execute_code"),
    ("oncall-skill",
     "Our checkout service is throwing errors in production right now and customers can't "
     "pay — what's the process to get this in front of on-call?",
     "skill"),
    ("notes-skill",
     "I've got some rough call notes that need to go out in our standard structure: we "
     "discussed the Q3 roadmap, decided to delay the launch two weeks, and Priya needs to "
     "update the deck by Monday.",
     "skill"),
]


def _load_env() -> dict:
    out: dict[str, str] = {}
    env_file = BUNDLE / ".env"
    if not env_file.exists():
        pytest.fail(f"{env_file} missing — run `make up` first")
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


@pytest.fixture(scope="module")
def env() -> dict:
    e = _load_env()
    for required in ("BOOTSTRAP_USER", "BOOTSTRAP_PASSWORD"):
        if not e.get(required):
            pytest.fail(f"{required} is not configured in bundle/.env — run `make up`")
    return e


@pytest.fixture(scope="module")
def chat_url(env) -> str:
    return f"http://localhost:{env.get('CHAT_PORT', '3080')}"


@pytest.fixture(scope="module")
def chat_client(env, chat_url):
    client = oidc_login.login(chat_url, env["BOOTSTRAP_USER"], env["BOOTSTRAP_PASSWORD"])
    refreshed = client.post(f"{chat_url}/api/auth/refresh")
    assert refreshed.status_code == 200, (
        f"session refresh failed ({refreshed.status_code}): {refreshed.text[:300]}"
    )
    token = refreshed.json().get("token")
    assert token, f"no access token in refresh response: {refreshed.text[:300]}"
    client.headers.update({
        "Authorization": f"Bearer {token}",
        "User-Agent": chat_turn.BROWSER_UA,
    })
    yield client
    client.close()


@pytest.fixture(scope="module")
def system_prompt() -> str:
    """The literal `promptPrefix` this item added to the deployed config — read out of
    the committed YAML, not duplicated here, so this test cannot silently drift from what
    the surface actually serves (see module docstring)."""
    config = yaml.safe_load(LIBRECHAT_YAML.read_text())
    specs = config.get("modelSpecs", {}).get("list", [])
    spec = next((s for s in specs if s.get("name") == "glm-5-2"), None)
    assert spec, "no 'glm-5-2' modelSpec in librechat.yaml — has it been renamed?"
    prefix = spec.get("preset", {}).get("promptPrefix")
    assert prefix and "Decide for yourself" in prefix, (
        f"glm-5-2's promptPrefix does not look like the tool-selection paragraph this "
        f"item added: {prefix!r}"
    )
    return prefix


def _tool_categories(reply) -> set[str]:
    """Which of the three tool categories this turn's persisted message actually invoked.

    Tool names measured directly against the pinned v0.8.7 image, not assumed from docs:
    `web_search` (chat_turn / test_web_search_grounding.py, established), `bash_tool` /
    `read_file` (tests/test_fakeprovider_execute_code.py — the execute_code capability
    expands into these two callable tools, not a tool literally named "execute_code"), and
    `skill` (`@librechat/agents/dist/cjs/tools/SkillTool.cjs`: `const SkillToolName =
    "skill"`).
    """
    categories = set()
    for call in chat_turn.tool_calls(reply):
        name = (call.get("tool_call") or {}).get("name") or ""
        if "web_search" in name:
            categories.add("web_search")
        if name in ("bash_tool", "read_file") or "execute_code" in name:
            categories.add("execute_code")
        if name == "skill":
            categories.add("skill")
    return categories


# ---------------------------------------------------------------------------------------
# The crash-survivable JSONL layer. Append-only, fsync'd per line, resumable on skip.
# ---------------------------------------------------------------------------------------

def _append_result(record: dict) -> None:
    """Append one call's result and fsync it before returning. This is the actual fix
    for the defect that killed two prior dispatches: a torn-down/SIGTERM'd process must
    never lose a result that had already come back from the model."""
    RESULTS_JSONL.parent.mkdir(parents=True, exist_ok=True)
    with open(RESULTS_JSONL, "a", encoding="utf-8") as fp:
        fp.write(json.dumps(record) + "\n")
        fp.flush()
        os.fsync(fp.fileno())


def _load_results() -> list[dict]:
    """Every row recorded so far. Tolerates a truncated final line — the exact shape a
    SIGTERM mid-write leaves behind — by skipping it rather than raising, so a partial
    file is always scoreable."""
    if not RESULTS_JSONL.exists():
        return []
    rows = []
    for line in RESULTS_JSONL.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _outcome(row: dict) -> str:
    """Three-way classification, not two. A timed-out/errored call (`selected` is null)
    is a latency/infrastructure outcome — `no-result` — and must never be counted as
    evidence about tool SELECTION. Only `correct` / `wrong-tool` say anything about
    whether the model reached for the right tool."""
    if row.get("error") is not None or row.get("selected") is None:
        return "no-result"
    return "correct" if row.get("pass") else "wrong-tool"


def _latest_by_run_index(rows, condition, prompt_id) -> dict[int, dict]:
    """The most recent recorded row per run_index for this (condition, prompt_id), file
    order = recency since the JSONL is append-only. A retried no-result call appends a
    SECOND row under the same run_index rather than a new one (see
    `_completed_run_indices`); this is what makes the retry's outcome the one that
    counts instead of double-counting the triple."""
    latest: dict[int, dict] = {}
    for r in rows:
        if r.get("condition") == condition and r.get("prompt_id") == prompt_id:
            latest[r["run_index"]] = r
    return latest


def _completed_run_indices(rows, condition, prompt_id) -> set[int]:
    """Which run_index values should be SKIPPED on the next invocation. A no-result
    (timeout/error) row does NOT block a retry of that same call — only a row whose
    latest attempt actually returned a reply (correct or wrong-tool) counts as done. This
    is what lets a later dispatch re-attempt exactly the two prompts that timed out
    without also blocking on their own retry if it happens to time out again."""
    latest = _latest_by_run_index(rows, condition, prompt_id)
    return {
        run_index for run_index, row in latest.items()
        if _outcome(row) != "no-result"
    }


def _run_condition(chat_client, chat_url, label, condition, prompt_prefix, runs_per_prompt):
    """Every (prompt, run) pair for one condition still missing from the JSONL. Already-
    recorded (condition, prompt_id, run_index) triples are skipped, so re-invoking this
    file resumes rather than restarts.

    Exceptions are caught PER RUN, not allowed to abort the whole measurement — a single
    hung or errored turn must not erase every other data point, and per operational guard
    9 this is not a retry: each attempt is independent and its outcome, including a
    failure, is recorded and reported.
    """
    existing = _load_results()
    for prompt_id, prompt, expected in PROMPTS:
        done = _completed_run_indices(existing, condition, prompt_id)
        for run_index in range(runs_per_prompt):
            if run_index in done:
                print(
                    f"    [{label}] {prompt_id} run {run_index + 1}/{runs_per_prompt} "
                    f"already recorded in {RESULTS_JSONL.name} -- skipping"
                )
                continue
            record = {
                "ts": time.time(),
                "condition": condition,
                "prompt_id": prompt_id,
                "prompt": prompt,
                "expected": expected,
                "run_index": run_index,
                "model": MODEL,
            }
            try:
                payload = chat_turn.build_payload(
                    prompt, MODEL, ENDPOINT_NAME, ENDPOINT_TYPE,
                    execute_code=True, web_search=True, skills_catalog=True,
                    prompt_prefix=prompt_prefix,
                )
                conversation_id, _ = chat_turn.start_turn(
                    chat_client, chat_url, payload, timeout=TURN_TIMEOUT,
                )
                reply = chat_turn.wait_for_reply(
                    chat_client, chat_url, conversation_id, timeout=TURN_TIMEOUT,
                )
                categories = sorted(_tool_categories(reply))
                record["conversation_id"] = conversation_id
                record["selected"] = categories
                record["pass"] = expected in categories
                record["error"] = None
                record["text_snippet"] = chat_turn.reply_text(reply)[:200]
            except Exception as exc:  # noqa: BLE001 — a caught failure IS a data point
                record["conversation_id"] = None
                record["selected"] = None
                record["pass"] = False
                record["error"] = str(exc)[:300]
                record["text_snippet"] = None
            # THE FIX: written and fsync'd before the loop even prints, let alone
            # continues to the next call. Nothing downstream of this line can lose it.
            _append_result(record)
            print(
                f"    [{label}] {prompt_id} run {run_index + 1}/{runs_per_prompt} "
                f"expected={expected!r} -> "
                f"{'ERROR: ' + record['error'] if record['error'] else record['selected']}"
            )


def _score(condition: str, runs_per_prompt: int) -> tuple[int, int, list[str], int]:
    """(prompts_passed, total_prompts, failure_notes, no_result_count) computed as a pure
    pass over whatever rows are ACTUALLY on disk for this condition right now — not
    against `runs_per_prompt`, which is only a target for how many NEW runs
    `_run_condition` attempts. Dedupes to the LATEST row per (prompt_id, run_index) first
    (see `_latest_by_run_index`) so a retried no-result call is scored once, on its
    retry's outcome, not twice.

    THREE OUTCOMES, NOT TWO: `correct`, `wrong-tool`, `no-result` (timeout/error,
    `selected: null`). A prompt passes the majority bar over the runs that RETURNED a
    reply (`correct` + `wrong-tool`) — a `no-result` run says nothing about tool
    selection and must not count against (or for) the bar. A prompt with zero returned
    runs cannot be scored on selection at all and is reported as such rather than
    silently failed or passed."""
    rows = _load_results()
    passed = 0
    notes = []
    total_no_result = 0
    for prompt_id, prompt, expected in PROMPTS:
        latest = _latest_by_run_index(rows, condition, prompt_id)
        prompt_rows = list(latest.values())
        no_result = sum(1 for r in prompt_rows if _outcome(r) == "no-result")
        returned = [r for r in prompt_rows if _outcome(r) != "no-result"]
        hits = sum(1 for r in returned if _outcome(r) == "correct")
        n_returned = len(returned)
        total_no_result += no_result
        ok = n_returned > 0 and hits > n_returned / 2
        if ok:
            passed += 1
        else:
            observed = [
                (r.get("selected") if r.get("error") is None else f"ERROR: {r.get('error')}")
                for r in prompt_rows
            ]
            reason = "no calls returned a reply (all timed out/errored)" if n_returned == 0 else (
                f"{hits}/{n_returned} returned runs matched (target was "
                f"{runs_per_prompt}/prompt)"
            )
            notes.append(
                f"FAILED: {prompt_id} ({prompt!r}) expected {expected!r} — {reason}; "
                f"no-result={no_result}; per-run: {observed}"
            )
    return passed, len(PROMPTS), notes, total_no_result


def _print_score_report(runs_per_prompt: int = RUNS_PER_PROMPT) -> None:
    print(f"\nRAW PER-CALL RESULTS: {RESULTS_JSONL}")
    for condition, label in (
        ("baseline", "BASELINE — no instructions of ours"),
        ("with-instructions", "WITH INSTRUCTIONS — this item's promptPrefix"),
    ):
        passed, total, notes, no_result = _score(condition, runs_per_prompt)
        print(
            f"\n{label}: {passed}/{total} prompts pass the majority bar "
            f"(selection rate over calls that returned a reply); "
            f"no-result (timeout/error) count: {no_result}"
        )
        for note in notes:
            print(f"  - {note}")


class TestUnpromptedToolSelection:
    def test_before_and_after_the_system_prompt(self, chat_client, chat_url, system_prompt):
        print(f"\nRAW PER-CALL RESULTS FILE: {RESULTS_JSONL}")
        print(
            f"RUNS_PER_PROMPT={RUNS_PER_PROMPT} (override with "
            f"TOOL_SELECTION_RUNS_PER_PROMPT; already-recorded runs are skipped, so "
            f"re-invoking only performs the calls still missing)"
        )

        print("\n" + "=" * 78)
        print("BASELINE — no instructions of ours (tools attached, no promptPrefix)")
        print("=" * 78)
        _run_condition(chat_client, chat_url, "baseline", "baseline", None, RUNS_PER_PROMPT)
        baseline_passed, total, baseline_notes, baseline_no_result = _score("baseline", RUNS_PER_PROMPT)

        print("\n" + "=" * 78)
        print("WITH INSTRUCTIONS — the promptPrefix this item added to librechat.yaml")
        print("=" * 78)
        _run_condition(
            chat_client, chat_url, "with-instructions", "with-instructions",
            system_prompt, RUNS_PER_PROMPT,
        )
        after_passed, _, after_notes, after_no_result = _score("with-instructions", RUNS_PER_PROMPT)

        print("\n" + "=" * 78)
        print(f"RESULT: baseline {baseline_passed}/{total} prompts selected the right tool "
              f"on the majority of their RETURNED runs (no-result={baseline_no_result}); "
              f"with instructions {after_passed}/{total} (no-result={after_no_result}).")
        print(f"Raw data: {RESULTS_JSONL}")
        print("=" * 78)
        if baseline_notes:
            print("\nBASELINE FAILURES (diagnostic, not asserted):")
            for note in baseline_notes:
                print(f"  - {note}")
        if after_notes:
            print("\nWITH-INSTRUCTIONS FAILURES (this IS part of done — recorded, not hidden):")
            for note in after_notes:
                print(f"  - {note}")
        print()

        # THE ITEM'S DONE CONDITION: with the system prompt in place, the assistant
        # selects the correct tool for AT LEAST THE CLEAR-CUT MAJORITY of the ~10
        # representative prompts — more than half, stated as a bar rather than "all of
        # them", because tool selection against a live model is stochastic (guard 9).
        # The baseline number above is reported, not gated on, per the module docstring:
        # there is no prior claim that unprompted selection worked at all.
        assert after_passed > total / 2, (
            f"only {after_passed}/{total} prompts selected the right tool on a majority "
            f"of recorded runs WITH the system prompt in place — see the per-prompt "
            f"failures printed above, and {RESULTS_JSONL}, for which ones and why"
        )


if __name__ == "__main__":
    # Score-only mode: no bundle, no live calls, safe to run at any time including
    # mid-sweep or right after a kill — exactly the "scoring is just a pass over the
    # JSONL, and it works on a partial file too" property this file exists to provide.
    _print_score_report()
