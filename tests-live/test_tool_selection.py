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
correctly" if the MAJORITY of its runs invoked the expected tool category — that is the
pass bar, stated here rather than papered over with a retry loop. The item's own DONE
condition ("at least the clear-cut majority" of ~10 prompts) is asserted only against the
WITH-INSTRUCTIONS condition; the no-instructions baseline is measured and printed for the
before/after comparison the item asks for, and is not itself a pass/fail gate — there is no
prior claim that unprompted tool selection worked at all before this item, so nothing
regresses if baseline is weak.

FAILURES ARE RECORDED, NOT HIDDEN. Every per-prompt result (each run's chosen tool
category, or the caught exception) is printed via a session-scoped summary at the end of
the test so a failing prompt names which one, and why, in -v output — this is the
"documented failure list" the item's DONE condition requires as part of done, not an
admission of failure.

Run (bundle must be up, with FORGE_API_KEY configured and the gateway catalogue
re-rendered against it — see bundle/bin/render-gateway-config.py):
  .venv-test/bin/pytest tests-live/test_tool_selection.py -v --tb=short -s -p no:cacheprovider
"""

from pathlib import Path

import pytest
import yaml

import chat_turn
import oidc_login

REPO = Path(__file__).resolve().parent.parent
BUNDLE = REPO / "bundle"
LIBRECHAT_YAML = BUNDLE / "librechat" / "librechat.yaml"

MODEL = "glm-5.2@deepinfra"
ENDPOINT_NAME = "Enterprise AI"
ENDPOINT_TYPE = "custom"

RUNS_PER_PROMPT = 3
TURN_TIMEOUT = 100.0

# Prompts, deliberately NOT naming a tool, a skill, or an instruction to search/compute.
# Each is tagged with the tool category a competent unprompted assistant reaches for.
# Two skill prompts is all the bundled tenant fixture corpus (bundle/skills/) supports —
# these are the SAME two skills tests/test_skill_corpus.py already uses as fixture content
# for manual invocation; using them again here to test AUTOMATIC invocation is reusing an
# existing test fixture, not coupling the platform to tenant content (guard #10 is about
# the SYSTEM PROMPT naming a skill, which the promptPrefix text above does not).
PROMPTS = [
    ("What is the latest stable release version of the Python programming language?",
     "web_search"),
    ("Who is the current CEO of OpenAI?",
     "web_search"),
    ("What is the newest LTS release line of Node.js right now?",
     "web_search"),
    ("What's the top story on Hacker News right now?",
     "web_search"),
    ("What is 384195 multiplied by 88213, exactly?",
     "execute_code"),
    ("What is the standard deviation of these numbers: 14, 8, 21, 3, 45, 9, 30, 17, 2, 26?",
     "execute_code"),
    ("Sort this list and give me the median: 55, 3, 89, 12, 47, 6, 71, 24",
     "execute_code"),
    ("Reverse the string 'enterpriseaiframeworkdogfood' and tell me the 7th character of "
     "the reversed string.",
     "execute_code"),
    ("Our checkout service is throwing errors in production right now and customers can't "
     "pay — what's the process to get this in front of on-call?",
     "skill"),
    ("I've got some rough call notes that need to go out in our standard structure: we "
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


def _run_condition(chat_client, chat_url, label, prompt_prefix):
    """Every (prompt, run) pair for one condition. Returns a list of per-prompt records:
    {"prompt", "expected", "runs": [{"categories": set|None, "error": str|None}, ...]}.

    Exceptions are caught PER RUN, not allowed to abort the whole measurement — a single
    hung or errored turn must not erase every other data point, and per operational guard
    9 this is not a retry: each attempt is independent and its outcome, including a
    failure, is recorded and reported.
    """
    records = []
    for prompt, expected in PROMPTS:
        runs = []
        for i in range(RUNS_PER_PROMPT):
            try:
                reply = chat_turn.send_turn(
                    chat_client, chat_url, prompt, model=MODEL,
                    endpoint=ENDPOINT_NAME, endpoint_type=ENDPOINT_TYPE,
                    web_search=True, execute_code=True, skills_catalog=True,
                    prompt_prefix=prompt_prefix, timeout=TURN_TIMEOUT,
                )
                text = chat_turn.reply_text(reply)
                categories = _tool_categories(reply)
                runs.append({"categories": categories, "error": None, "text": text[:200]})
            except Exception as exc:  # noqa: BLE001 — a caught failure IS a data point
                runs.append({"categories": None, "error": str(exc)[:300], "text": None})
            print(
                f"    [{label}] run {i + 1}/{RUNS_PER_PROMPT} expected={expected!r} "
                f"-> {runs[-1]['categories'] if runs[-1]['error'] is None else 'ERROR: ' + runs[-1]['error']}"
            )
        records.append({"prompt": prompt, "expected": expected, "runs": runs})
    return records


def _score(records) -> tuple[int, int, list[str]]:
    """(prompts_passed, total_prompts, failure_notes). A prompt passes the majority bar
    (operational guard 9's explicit pass bar, not a retry) if MORE THAN HALF its runs
    invoked the expected category."""
    passed = 0
    notes = []
    for rec in records:
        hits = sum(
            1 for run in rec["runs"]
            if run["categories"] is not None and rec["expected"] in run["categories"]
        )
        total = len(rec["runs"])
        ok = hits > total / 2
        if ok:
            passed += 1
        else:
            observed = [
                sorted(run["categories"]) if run["categories"] is not None
                else f"ERROR: {run['error']}"
                for run in rec["runs"]
            ]
            notes.append(
                f"FAILED: {rec['prompt']!r} expected {rec['expected']!r}, "
                f"got {hits}/{total} runs matching; per-run results: {observed}"
            )
    return passed, len(records), notes


class TestUnpromptedToolSelection:
    def test_before_and_after_the_system_prompt(self, chat_client, chat_url, system_prompt):
        print("\n" + "=" * 78)
        print("BASELINE — no instructions of ours (tools attached, no promptPrefix)")
        print("=" * 78)
        baseline = _run_condition(chat_client, chat_url, "baseline", prompt_prefix=None)
        baseline_passed, total, baseline_notes = _score(baseline)

        print("\n" + "=" * 78)
        print("WITH INSTRUCTIONS — the promptPrefix this item added to librechat.yaml")
        print("=" * 78)
        after = _run_condition(chat_client, chat_url, "with-instructions",
                                prompt_prefix=system_prompt)
        after_passed, _, after_notes = _score(after)

        print("\n" + "=" * 78)
        print(f"RESULT: baseline {baseline_passed}/{total} prompts selected the right tool "
              f"on the majority of their {RUNS_PER_PROMPT} runs; "
              f"with instructions {after_passed}/{total}.")
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
            f"of runs WITH the system prompt in place — see the per-prompt failures "
            f"printed above for which ones and why"
        )
