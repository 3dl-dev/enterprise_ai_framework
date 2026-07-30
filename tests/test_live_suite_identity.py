"""No live test may sign in as a person, and the gate that enforces it must actually hold.

WHAT WENT WRONG (enterpriseaiframework-cf5)

Four suites in tests-live/ resolved their identity like this:

    _secret("workspace-user-student", "USERNAME"), _secret("workspace-user-student", "PASSWORD")

`student` is a person. Opening the Code tab drives that account's OWN workspace pod:
switching to it starts a session, rewrites `.meta/<project>.session` and clears the
`.new-session` flag. A measured run changed both — it can silently convert somebody's
session. test_workspace.py went further and typed into two people's shells, ran `make test`
there, deleted `.pytest_cache` and let aider rewrite and commit `app.py`.

Every one of those files carried a comment saying not to run it while anyone was signed in.
The comment was there and a run happened anyway. That is the whole reason this file exists:
a warning in a docstring is not a mechanism, and the fix is only real if something fails the
build when it regresses.

WHY THE CHECK LIVES IN THE HERMETIC SUITE

It has to run when nobody is thinking about live tests — on every `make test`, with no
cluster, no credential and no browser. A guard that only runs in the suite it is guarding is
not a guard. So this reads source and drives pytest's own collection; it never signs in to
anything.

THREE CLAIMS, AND THE ORDER MATTERS

  1. The credential read is centralised. Only live_identity.py may name a Secret that holds
     somebody's login. Copy-paste is how one read became four.
  2. The gate holds at COLLECTION. Every test in a module that resolves a live identity is
     removed from the run when no account is named — before any fixture executes, because a
     session-scoped fixture arms the hazard as soon as the file is collected, even for an
     unrelated test.
  3. The hermetic majority still runs. This is the claim that makes 1 and 2 worth having:
     deselecting everything would satisfy them both and deliver nothing. So the count that
     runs with NO account is pinned, not just the count that does not.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
LIVE = REPO / "tests-live"
GUARD = LIVE / "live_identity.py"
ENSURE_USER_SH = REPO / "deploy/bin/ensure-second-user.sh"

# Secret names that hold a human login. `workspace-user-<name>` is one account's own
# password; `workspace-test-user` is the shared indirection that made `student` the test
# identity without anybody deciding to.
_LOGIN_SECRET_PREFIXES = ("workspace-user-", "workspace-test-user")

# The deployment Secret plus the field that is the operator's password. The Secret itself is
# fine to read — PUBLIC_BASE_URL, GATEWAY_MASTER_KEY and the admin token all live in it and
# none of them is a person — so the pair is what is disallowed, not the name.
_OPERATOR_PASSWORD_FIELDS = ("BOOTSTRAP_PASSWORD",)

#: The functions that hand back a human credential. A module calling any of them is a module
#: whose every test must be gated.
_IDENTITY_CALLS = ("account", "second_account", "operator_account")


def _live_modules() -> list[Path]:
    return sorted(p for p in LIVE.glob("*.py") if p.name != "live_identity.py")


def _non_docstring_strings(tree: ast.AST) -> list[str]:
    """Every string literal in the tree that is not a docstring.

    Docstrings are excluded because this file's own subject has to be DESCRIBED somewhere,
    and the modules that were fixed each explain in prose which Secret they used to read.
    A check that could not tell prose from code would force those explanations to be deleted,
    which is the opposite of what is wanted. Comments never appear in an AST at all.

    Implemented as "a string that is the whole of an expression statement", which is exactly
    what a docstring is — and also what a stray bare string is, which is harmless.
    """
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            docstrings.add(id(node.value))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and id(n) not in docstrings]


# ---------------------------------------------------------------- 1. centralised


def test_only_the_guard_module_may_name_a_secret_holding_a_persons_login():
    """The read is in one place, so there is one place to gate.

    This is the assertion that fails on the pre-fix tree: test_browser.py,
    test_e2e_journey.py, test_portal.py, test_workspace.py and test_memory.py each contained
    `"workspace-user-student"` or `"workspace-test-user"` as a live string.
    """
    offenders: list[str] = []
    for path in _live_modules():
        for text in _non_docstring_strings(ast.parse(path.read_text())):
            if any(text.startswith(p) for p in _LOGIN_SECRET_PREFIXES):
                offenders.append(f"{path.name}: {text!r}")
    assert not offenders, (
        "these modules name a Secret that holds a person's login. Resolve identity through "
        "tests-live/live_identity.py instead — it refuses any account that is not both named "
        "by EAI_LIVE_TEST_USER and marked THROWAWAY:\n  " + "\n  ".join(offenders)
    )


def test_only_the_guard_module_may_read_the_operators_password_from_the_cluster():
    """BOOTSTRAP_PASSWORD out of the CLUSTER Secret is the founder's live login.

    Narrowed to the cluster on purpose, and the distinction is real rather than a convenience:
    `bundle/.env`'s BOOTSTRAP_PASSWORD is the compose bundle's own bootstrap account, minted
    locally by `make up` and belonging to nobody. test_mcp_echo.py reads that one and is
    correct to. What is disallowed is `enterprise-ai-secrets` + `BOOTSTRAP_PASSWORD`, which
    is the deployed realm's operator.

    KNOWN AND NOT FIXED HERE: test_memory.py reads the bundle's BOOTSTRAP_* and signs in with
    it against the CLUSTER, so if the two are the same credential it is still a login as the
    operator. This check cannot see that, and narrowing it further would produce false
    positives on the compose suites. The module is gated `needs_real_user` +
    `mutates_live_deployment` so it cannot run unattended, and the residue is recorded in
    dogfood-findings.md rather than papered over.
    """
    offenders = []
    for path in _live_modules():
        strings = _non_docstring_strings(ast.parse(path.read_text()))
        if "enterprise-ai-secrets" not in strings:
            continue
        for field in _OPERATOR_PASSWORD_FIELDS:
            if field in strings:
                offenders.append(f"{path.name}: enterprise-ai-secrets/{field}")
    assert not offenders, (
        "these modules read the operator's own password out of the cluster Secret. Use "
        "live_identity.operator_account(), which requires EAI_LIVE_TEST_OPERATOR to name "
        "them explicitly:\n  " + "\n  ".join(offenders)
    )


def test_the_guard_itself_requires_both_an_explicit_name_and_a_throwaway_marker():
    """The two gates, read out of the guard's own source.

    Deliberately labelled as what it is: a SOURCE check, not a behavioural one — there is no
    cluster here to hold a Secret. What it defends against is somebody deciding the env var
    is enough and deleting the marker check, which would put the old hazard one command
    away. The behaviour of both gates is exercised for real by the two tests below and by
    live_identity's own failure messages.
    """
    src = GUARD.read_text()
    assert "THROWAWAY" in src, (
        "live_identity.py no longer checks for the THROWAWAY marker, so naming a real "
        "person in EAI_LIVE_TEST_USER would be enough to sign in as them"
    )
    assert 'os.environ.get(env, "")' in src or "os.environ.get(ENV" in src, \
        "live_identity.py no longer reads the account name from the environment"
    # No default. `EAI_LIVE_TEST_USER` falling back to anything is the original defect.
    assert 'os.environ.get(env, "student")' not in src
    assert "workspace-user-student" not in _non_docstring_strings(ast.parse(src)), (
        "live_identity.py hardcodes a person's Secret"
    )


def test_the_provisioning_script_can_actually_mark_an_account_throwaway():
    """A gate nothing can satisfy is a gate that gets deleted.

    Source check, and said plainly: running the script needs a cluster. What it pins is that
    `--throwaway` exists and writes the field live_identity looks for — if those two drift
    apart, the marked suites become permanently unrunnable and the pressure goes on the
    guard rather than on the script.
    """
    src = ENSURE_USER_SH.read_text()
    assert "--throwaway" in src, "ensure-second-user.sh cannot mark an account throwaway"
    assert "THROWAWAY" in src
    assert "--from-literal=THROWAWAY=" in src, (
        "ensure-second-user.sh accepts --throwaway but never writes the field "
        "live_identity.py reads"
    )


def test_nothing_repoints_the_shared_test_user_secret_any_more():
    """The indirection that made a person the test identity is gone, not merely unused.

    `workspace-test-user` meant "the account tests-live signs in as", and
    ensure-second-user.sh claimed it for whichever user it was asked to create. Nothing has
    to be wrong for that to point the suites at a camper. It may still be READ once (to keep
    a pre-split account's password valid); it must never be written.
    """
    src = ENSURE_USER_SH.read_text()
    writes = [line.strip() for line in src.splitlines()
              if "workspace-test-user" in line and "create secret" in line]
    assert not writes, (
        "ensure-second-user.sh still writes the shared test-identity Secret:\n  "
        + "\n  ".join(writes)
    )


# ---------------------------------------------------------------- 2 & 3. the gate holds


def _collect(env_extra: dict[str, str]) -> tuple[set[str], set[str]]:
    """(selected, deselected) node ids for tests-live, as pytest itself resolves them.

    Runs the real collection in a subprocess rather than reasoning about decorators. A module
    can be gated by `pytestmark`, by a per-test marker, or by a marker on a parametrised
    test, and a static reading of the source would have to reimplement all three — which is
    the class of mistake this whole item is about.

    Nothing here contacts a cluster: fixtures do not execute during collection, which is
    also precisely why the gate has to be a collection hook.
    """
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", **env_extra}
    for key in ("EAI_LIVE_TEST_USER", "EAI_LIVE_TEST_USER_2", "EAI_LIVE_TEST_OPERATOR",
                "EAI_LIVE_MAINTENANCE_WINDOW"):
        if key not in env_extra:
            env.pop(key, None)

    r = subprocess.run(
        [sys.executable, "-m", "pytest", "tests-live/", "--collect-only", "-q",
         "-p", "no:cacheprovider"],
        cwd=REPO, capture_output=True, text=True, timeout=600, env=env,
    )
    if "error" in r.stdout.lower() and "::" not in r.stdout:
        pytest.fail(f"collecting tests-live failed:\n{r.stdout[-3000:]}\n{r.stderr[-2000:]}")

    selected = {line.strip() for line in r.stdout.splitlines()
                if line.strip().startswith("tests-live/") and "::" in line}
    assert selected or env_extra, f"collected nothing at all:\n{r.stdout[-2000:]}"

    r_all = subprocess.run(
        [sys.executable, "-m", "pytest", "tests-live/", "--collect-only", "-q",
         "-p", "no:cacheprovider", "--no-header"],
        cwd=REPO, capture_output=True, text=True, timeout=600,
        env={**os.environ, "EAI_LIVE_TEST_USER": "collect-only-not-a-real-account",
             "EAI_LIVE_TEST_USER_2": "collect-only-not-a-real-account-2",
             "EAI_LIVE_TEST_OPERATOR": "collect-only-not-a-real-operator",
             "EAI_LIVE_MAINTENANCE_WINDOW": "1", "PYTHONDONTWRITEBYTECODE": "1"},
    )
    every = {line.strip() for line in r_all.stdout.splitlines()
             if line.strip().startswith("tests-live/") and "::" in line}
    return selected, every - selected


def _modules_that_resolve_a_live_identity() -> set[str]:
    """Derived from the source, not listed by hand, so a new suite cannot be forgotten."""
    out = set()
    for path in _live_modules():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in _IDENTITY_CALLS \
                    and isinstance(node.value, ast.Name) and node.value.id == "live_identity":
                out.add(path.name)
    return out


@pytest.fixture(scope="module")
def collected():
    return _collect({})


def test_at_least_one_suite_still_resolves_a_live_identity_through_the_guard():
    """If nothing calls the guard, everything below is vacuous.

    The failure this defends against is the lazy fix: delete the live suites, or route round
    live_identity, and every other assertion in this file passes while nothing is proved.
    """
    assert _modules_that_resolve_a_live_identity(), (
        "no module in tests-live calls live_identity.account() any more — either the live "
        "suites were deleted or the guard was bypassed; neither should pass silently"
    )


_UNMARKED_PROBE = '''
import pytest
import live_identity


@pytest.fixture()
def creds(request):
    return live_identity.account(request)


def test_unmarked_test_reaching_for_a_credential(creds):
    assert creds
'''

_MARKED_PROBE = '''
import pytest
import live_identity

pytestmark = pytest.mark.needs_real_user


@pytest.fixture()
def creds(request):
    return live_identity.account(request)


def test_marked_test_reaching_for_a_credential(creds):
    assert creds
'''

# A session-scoped credential fixture — the real suites' shape — shared by a marked test and
# an unmarked one. The marked test sets it up and the check runs; the unmarked test gets the
# CACHED value and pytest never re-enters the fixture body, so nothing inside live_identity
# can see it happen. This is the hole the conftest hookwrappers exist to close.
#
# `account` is monkeypatched at the _account_from level so the probe needs no cluster: the
# marker logic and the served-credential ledger are what is under test, not kubectl.
_CACHED_REUSE_PROBE = '''
import pytest
import live_identity

live_identity._account_from = lambda env: (
    live_identity.served.append(env + "=probe-account") or ("probe-account", "probe-password")
)


@pytest.fixture(scope="session")
def creds(request):
    return live_identity.account(request)


@pytest.mark.needs_real_user
def test_marked_goes_first(creds):
    assert creds[0] == "probe-account"


def test_unmarked_reuses_the_cached_credential(creds):
    assert creds[0] == "probe-account"
'''


def _run_probe(tmp_path, source: str, env_extra: dict[str, str]):
    """Write a one-test module into tests-live and run it. Returns the CompletedProcess.

    Written INTO tests-live rather than a temp directory because the gate is implemented in
    that directory's conftest.py, and a probe that did not load it would be testing nothing.
    Named with a `probe_` prefix so it is never collected by a normal run, and removed in a
    finally.
    """
    probe = LIVE / f"probe_{abs(hash(source)) % 10**8}_test.py"
    probe.write_text(source)
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", **env_extra}
    for key in ("EAI_LIVE_TEST_USER", "EAI_LIVE_TEST_USER_2", "EAI_LIVE_TEST_OPERATOR",
                "EAI_LIVE_MAINTENANCE_WINDOW"):
        if key not in env_extra:
            env.pop(key, None)
    try:
        return subprocess.run(
            [sys.executable, "-m", "pytest", str(probe.relative_to(REPO)), "-q",
             "-p", "no:cacheprovider"],
            cwd=REPO, capture_output=True, text=True, timeout=300, env=env,
        )
    finally:
        probe.unlink(missing_ok=True)


def test_an_unmarked_test_cannot_get_a_credential_at_all(tmp_path):
    """Claim 2, half one, proved by running it.

    A new test — or an old one that lost its marker — reaching for a live account must fail
    rather than sign in. This is the direction a deselection hook cannot cover: an unmarked
    test is not deselected by anything, so on a bare `pytest tests-live/` it would run.

    No cluster is contacted: the marker check happens before any kubectl, which is also why
    the failure is safe to provoke here.
    """
    r = _run_probe(tmp_path, _UNMARKED_PROBE, {})
    assert r.returncode != 0, (
        "an unmarked test was handed a live credential:\n" + r.stdout[-3000:]
    )
    assert "needs_real_user" in r.stdout, (
        "it failed, but not for the right reason — the marker check is what must stop it:\n"
        + r.stdout[-3000:]
    )
    # And specifically NOT because it got as far as looking for a Secret. Failing on a
    # missing env var or a kubectl error would be the same exit code and a different, weaker
    # guarantee: it would mean the guard depends on the environment being unconfigured.
    assert "THROWAWAY" not in r.stdout and "EAI_LIVE_TEST_USER is not set" not in r.stdout, (
        "the unmarked test got past the marker check and into the credential lookup:\n"
        + r.stdout[-3000:]
    )


def test_a_marked_test_is_held_back_rather_than_run(tmp_path):
    """Claim 2, half two. Marked plus no account named means it never executes.

    Deselected, not skipped: a skip is a green line that reads as coverage, and this suite has
    been fooled by exactly that shape before.

    The exit code is 5 — pytest's NO_TESTS_COLLECTED — because the probe module's only test is
    the gated one. That is asserted rather than tolerated, and it is the desired behaviour: a
    run that held everything back must not exit 0, or `make test-e2e` during a busy hour would
    print a green line having executed nothing. Measured, not assumed; the first version of
    this assertion expected 0 and was wrong.
    """
    r = _run_probe(tmp_path, _MARKED_PROBE, {})
    assert r.returncode == 5, (
        "a marked test with no account named should be held back and leave nothing to run "
        f"(pytest exit 5); got exit {r.returncode}:\n" + r.stdout[-3000:]
    )
    assert "1 deselected" in r.stdout, (
        f"expected the marked test to be deselected; pytest said:\n{r.stdout[-2000:]}"
    )
    assert "passed" not in r.stdout and "skipped" not in r.stdout, (
        "the marked test reported a pass or a skip, so something ran instead of being held "
        f"back — a skip is a green line that reads as coverage:\n{r.stdout[-2000:]}"
    )
    # The operator is told, in the run's own output, that something was withheld and how to
    # run it. Silent deselection is how a gate becomes a way of losing tests.
    assert "held back by the needs_real_user gate" in r.stdout, (
        f"the run did not say anything was withheld:\n{r.stdout[-2000:]}"
    )


def test_an_unmarked_test_cannot_reuse_a_cached_credential(tmp_path):
    """The hole the marker check alone cannot see, and the reason there are two mechanisms.

    A session-scoped credential fixture is set up once. Its marker check therefore runs once,
    for whichever test asked first. Every later test that depends on it gets the cached value
    without pytest re-entering the fixture body — so an unmarked test sharing a session-scoped
    `account` fixture with a marked one would sign in to the live deployment and nothing
    inside live_identity would be called to notice.

    This is not hypothetical: `account` in test_browser.py, `users` in test_workspace.py and
    `creds` in test_portal.py are all broader than function scope, because the fixtures that
    depend on them are.

    The marked probe test must PASS and the unmarked one must ERROR at setup — errored rather
    than failed because the backstop stops it before its fixtures run, so it never holds a
    credential at all. Both halves are asserted: a mechanism that blocked the marked test too
    would be useless in the same way deleting the suite would be.
    """
    r = _run_probe(tmp_path, _CACHED_REUSE_PROBE, {
        "EAI_LIVE_TEST_USER": "probe-account",
        "EAI_LIVE_MAINTENANCE_WINDOW": "1",
    })
    assert r.returncode != 0, (
        "an unmarked test reused a cached live credential and nothing failed:\n"
        + r.stdout[-3000:]
    )
    assert "1 passed" in r.stdout and "1 error" in r.stdout, (
        "expected the marked probe to pass and the unmarked one to error at setup; pytest "
        f"said:\n{r.stdout[-3000:]}"
    )
    assert "test_unmarked_reuses_the_cached_credential" in r.stdout
    assert "test_marked_goes_first" not in r.stdout.split("short test summary")[-1], (
        "the marked test was blocked too, so the backstop is refusing everything:\n"
        + r.stdout[-3000:]
    )
    assert "is not marked" in r.stdout, (
        f"it failed for some other reason than the marker:\n{r.stdout[-3000:]}"
    )
    # No plugin warning: a guard that trips pytest's own "raised during teardown" warning is
    # reporting the right outcome through the wrong channel.
    assert "PluggyTeardownRaisedWarning" not in r.stdout, (
        f"the backstop raised from a hookwrapper teardown:\n{r.stdout[-2000:]}"
    )


def test_no_test_that_needs_a_live_account_is_selected_by_default(collected):
    """Claim 2, at the level of the real suites rather than a probe.

    Cross-checked against the marker rather than against module membership: test_browser.py
    and test_workspace_isolation.py each contain BOTH hermetic tests and gated ones, which is
    the desired end state, so "this module touches identity" is the wrong unit. What must hold
    is that nothing carrying the marker survives collection with no account named.
    """
    selected, deselected = collected
    assert deselected, "nothing is gated at all, so the marker is not wired to anything"
    assert not (selected & deselected)


def test_the_gate_opens_when_an_account_is_named(collected):
    """The other direction, which matters just as much.

    A gate that never opens is a suite that has been deleted with extra steps. If naming an
    account does not bring the marked tests back, the coverage this item promised to keep is
    gone and nobody would find out until they needed it.
    """
    _, deselected = collected
    assert deselected, "nothing is gated at all, so the marker is not wired to anything"
    opened, _ = _collect({
        "EAI_LIVE_TEST_USER": "collect-only-not-a-real-account",
        "EAI_LIVE_TEST_USER_2": "collect-only-not-a-real-account-2",
        "EAI_LIVE_TEST_OPERATOR": "collect-only-not-a-real-operator",
        "EAI_LIVE_MAINTENANCE_WINDOW": "1",
    })
    still_held = sorted(deselected - opened)
    assert not still_held, (
        "naming an account did not bring these back, so they are unreachable rather than "
        "gated:\n  " + "\n  ".join(still_held)
    )


# The two in test_browser.py that genuinely cannot be hosted: they need ttyd's own xterm.js
# reporting what IT fitted to, and a real opencode resolving a real model through the
# gateway. Pinned by name because this file is the item's deliverable and a THIRD name
# appearing here means somebody quietly moved a test out of the hermetic set instead of
# hosting it — which is exactly the trade the item exists to prevent.
_BROWSER_NEEDS_A_POD = {
    "test_the_agent_actually_boots_in_the_terminal",
    "test_the_terminal_keeps_its_size_across_a_reconnect",
}

# What runs with no account, no cluster and no credential. A floor, not an equality: adding
# hermetic coverage must not fail the build. Dropping below it means coverage was traded for
# hermeticity, which is the failure mode the item named explicitly.
_BROWSER_HERMETIC_FLOOR = 15


def test_the_browser_suite_is_hermetic_apart_from_the_two_that_need_ttyd(collected):
    """Claim 3, and the point of the exercise.

    `make test-browser` used to need a real person signed out and a real cluster up. It now
    runs its portal, its settings sheet, its workshop and both failure cases against a
    loopback stack. If that stops being true, this fails.
    """
    selected, deselected = collected
    hermetic = {n.split("::")[-1] for n in selected if "test_browser.py" in n}
    gated = {n.split("::")[-1].split("[")[0] for n in deselected if "test_browser.py" in n}

    assert gated == _BROWSER_NEEDS_A_POD, (
        f"the set of browser tests needing a real pod changed to {sorted(gated)}. Expected "
        f"{sorted(_BROWSER_NEEDS_A_POD)}. If a test genuinely cannot be hosted, say why in "
        "its docstring and update this set; do not gate one to make it pass."
    )
    assert len(hermetic) >= _BROWSER_HERMETIC_FLOOR, (
        f"only {len(hermetic)} browser tests run without a live account, down from "
        f"{_BROWSER_HERMETIC_FLOOR}. Coverage was traded for hermeticity:\n  "
        + "\n  ".join(sorted(hermetic))
    )
    # And the ones this item converted are in the hermetic set by NAME, not merely by count —
    # a count is satisfied by any fifteen tests, including fifteen new trivial ones.
    for name in ("test_portal_panels_actually_populate",
                 "test_portal_shows_the_signed_in_user",
                 "test_portal_key_rotation_dialog_opens_and_can_be_dismissed",
                 "test_workshop_terminal_is_the_hero_with_the_drawer_shut",
                 "test_workshop_renders_with_no_javascript_errors"):
        assert any(n.startswith(name) for n in hermetic), (
            f"{name} no longer runs without a live account"
        )
