"""Live tests against Forge. These spend real money (fractions of a cent per run).

Deliberately outside `tests/`, which pytest.ini scopes to the hermetic suite. The nine
scope items must stay provable with no provider account and no spend — that is item 8 —
so anything that talks to a real upstream lives here and runs only when asked, via
`make test-forge`.

Missing credentials fail loudly rather than skipping. A skipped test that silently
reports success is how you end up believing an upstream works when nobody has checked.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

import live_identity

BUNDLE = Path(__file__).resolve().parent.parent / "bundle"


# ---------------------------------------------------------------- the two live-state gates
#
# Deselection, not skipping, and not a warning in a docstring.
#
# The suites in this directory used to reach a real person's credential from a
# session-scoped fixture, which means the hazard was armed by COLLECTING the file — running
# one unrelated test in it was enough. So the gate is applied at collection: with neither
# env var set, every test that needs a live account or writes deployment-wide state is
# removed from the run before any fixture executes.
#
# Deselected rather than skipped because a skip is a green line that reads as coverage.
# Deselection prints a count, and the header below says exactly what was held back and
# what would let it run. See live_identity.py for why an env var alone is not the whole
# gate.


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        f"{live_identity.MARK_REAL_USER}: signs in as, or drives the pod of, an account on "
        f"the live deployment. Needs {live_identity.ENV_USER}.",
    )
    config.addinivalue_line(
        "markers",
        f"{live_identity.MARK_MUTATES}: writes cluster state a live user depends on. "
        f"Needs {live_identity.ENV_MAINTENANCE}.",
    )


_HELD: dict[str, int] = {}


def pytest_collection_modifyitems(config, items):
    have_user = bool(live_identity.requested_user())
    window = live_identity.maintenance_window()

    keep, drop = [], []
    for item in items:
        needs_user = item.get_closest_marker(live_identity.MARK_REAL_USER) is not None
        mutates = item.get_closest_marker(live_identity.MARK_MUTATES) is not None
        if (needs_user and not have_user) or (mutates and not window):
            drop.append(item)
            if needs_user and not have_user:
                _HELD[live_identity.MARK_REAL_USER] = \
                    _HELD.get(live_identity.MARK_REAL_USER, 0) + 1
            if mutates and not window:
                _HELD[live_identity.MARK_MUTATES] = \
                    _HELD.get(live_identity.MARK_MUTATES, 0) + 1
        else:
            keep.append(item)

    if drop:
        config.hook.pytest_deselected(items=drop)
        items[:] = keep


def pytest_report_header(config):
    lines = []
    if not live_identity.requested_user():
        lines.append(
            f"live identity: {live_identity.ENV_USER} unset — tests marked "
            f"{live_identity.MARK_REAL_USER} are deselected. They sign in as a real account "
            "and drive its workspace pod; the hermetic majority runs without one."
        )
    else:
        lines.append(
            f"live identity: {live_identity.ENV_USER}="
            f"{live_identity.requested_user()} (must be marked THROWAWAY)"
        )
    if not live_identity.maintenance_window():
        lines.append(
            f"live mutation: {live_identity.ENV_MAINTENANCE} unset — tests marked "
            f"{live_identity.MARK_MUTATES} are deselected."
        )
    return lines


# ---------------------------------------------------------------- the backstop
#
# The marker check inside live_identity has one hole, and it is a caching hole rather than a
# logic one: a SESSION-scoped credential fixture is set up ONCE, for the first test that asks.
# Its marker check therefore runs once. A second, UNMARKED test depending on the same fixture
# gets the cached credential and pytest never re-enters the fixture body, so nothing in
# live_identity is called and nothing can notice.
#
# That is not hypothetical. `account` in test_browser.py, `users` in test_workspace.py and
# `creds` in test_portal.py are all broader than function scope, because the fixtures that
# depend on them are.
#
# What survives caching is the fixture CLOSURE. live_identity records the NAME of every
# fixture that resolved a live identity; `item.fixturenames` names that fixture for every
# consumer whether the value was cached or not. So this fails an unmarked test that merely
# DEPENDS on such a fixture, which is the claim that actually matters.
#
# Ordering is covered both ways: unmarked-first is caught by live_identity's own check when
# the fixture body runs, marked-first-then-unmarked is caught here.
#
# MEASURED: the first version of this watched a counter of credentials served and the reuse
# probe walked straight through it. tests/test_live_suite_identity.py has that probe.


def pytest_runtest_setup(item):
    """Refuse before the fixtures run, not after.

    A plain hook rather than a hookwrapper, deliberately. `item.fixturenames` is resolved at
    collection, so the closure is already known here — which means an unmarked test is stopped
    BEFORE its fixtures execute and never touches a credential at all. Wrapping the hook and
    checking after the yield also worked, but it let setup complete first and pytest reported
    it as a plugin raising during teardown, which is a worse signal for the same outcome.
    """
    used = live_identity.credential_fixtures & set(item.fixturenames)
    if not used:
        return
    if item.get_closest_marker(live_identity.MARK_REAL_USER) is None:
        pytest.fail(
            f"{item.nodeid} depends on {', '.join(sorted(used))}, which resolves a live "
            f"account, but is not marked `{live_identity.MARK_REAL_USER}`.\n\n"
            "If that fixture is broader than function scope the credential was CACHED, so "
            "live_identity's own check did not re-run — this is the backstop for exactly "
            "that case. Mark this test too, or give it a fixture that does not resolve a "
            "live identity.",
            pytrace=False,
        )


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    for mark, count in sorted(_HELD.items()):
        env = (live_identity.ENV_USER if mark == live_identity.MARK_REAL_USER
               else live_identity.ENV_MAINTENANCE)
        terminalreporter.write_line(
            f"{count} test(s) held back by the {mark} gate; set {env} to run them "
            f"(see tests-live/live_identity.py)."
        )


def _env() -> dict:
    out = {}
    env_file = BUNDLE / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip()
    out.update({k: v for k, v in os.environ.items() if k.startswith("FORGE_")})
    return out


@pytest.fixture(scope="session")
def env() -> dict:
    e = _env()
    for required in ("FORGE_BASE_URL", "FORGE_API_KEY", "FORGE_ACCOUNT_ID"):
        if not e.get(required):
            pytest.fail(
                f"{required} is not configured in bundle/.env — "
                "run `make forge-config` to load it from 1Password"
            )
    return e


@pytest.fixture(scope="session")
def forge_url(env) -> str:
    return env["FORGE_BASE_URL"].rstrip("/")


@pytest.fixture(scope="session")
def forge_headers(env) -> dict:
    return {"Authorization": f"Bearer {env['FORGE_API_KEY']}"}


@pytest.fixture(scope="session")
def gateway_url(env) -> str:
    return f"http://localhost:{env.get('GATEWAY_PORT', '4000')}"


@pytest.fixture(scope="session")
def virtual_key(env, gateway_url) -> str:
    """A dedicated virtual key, minted for this run and revoked after.

    Traffic must go surface -> virtual key -> our gateway -> Forge; using the gateway
    master key would skip exactly the part we built.

    It mints its own rather than borrowing the chat surface's key from .env, because the
    hermetic suite's exit-path test revokes every key and re-mints them. Sharing that key
    made this suite fail intermittently when run after `make test` — which is the obvious
    order to run them in, and an intermittent failure that clears on a rerun is the worst
    kind: it teaches you to rerun instead of to look.
    """
    import httpx

    alias = "forge-live-test::terminal"
    master = {"Authorization": f"Bearer {env['GATEWAY_MASTER_KEY']}"}

    # Clear any leftover from an interrupted run so the alias is never doubly minted.
    httpx.post(f"{gateway_url}/key/delete", headers=master,
               json={"key_aliases": [alias]}, timeout=60)

    created = httpx.post(
        f"{gateway_url}/key/generate", headers=master,
        json={"key_alias": alias, "metadata": {"surface": "terminal", "issuer": "live-tests"}},
        timeout=60,
    )
    if created.status_code != 200:
        pytest.fail(f"could not mint a virtual key ({created.status_code}); is `make up` done? "
                    f"{created.text[:200]}")

    yield created.json()["key"]

    httpx.post(f"{gateway_url}/key/delete", headers=master,
               json={"key_aliases": [alias]}, timeout=60)


@pytest.fixture(scope="session")
def control_plane_url(env) -> str:
    return f"http://localhost:{env.get('CONTROL_PLANE_PORT', '8081')}"


@pytest.fixture(scope="session")
def admin_headers(env) -> dict:
    return {"Authorization": f"Bearer {env['CONTROL_PLANE_ADMIN_TOKEN']}"}


@pytest.fixture(scope="session")
def forge_admin_key(env) -> str:
    """Setup-time credential, fetched on demand and never persisted.

    Needed to read /v1/usage and /v1/pricing, which is how these tests reconcile our
    computed cost against what Forge actually billed.
    """
    if env.get("FORGE_ADMIN_KEY"):
        return env["FORGE_ADMIN_KEY"]
    r = subprocess.run(
        ["op", "item", "get", "Forge / enterprise-ai-framework",
         "--vault", "3dl-ops", "--fields", "FORGE_ADMIN_KEY", "--reveal"],
        capture_output=True, text=True,
    )
    if r.returncode != 0 or not r.stdout.strip():
        pytest.fail(
            "Forge admin key unavailable; sign in with `op signin`. "
            "It is needed to read usage for the reconciliation check."
        )
    return r.stdout.strip()


def forge_usage(forge_url: str, admin_key: str, account_id: str) -> list[dict]:
    """All usage events for the account.

    Queried without `since`/`until` on purpose: those filters are a known Forge bug
    (forge-f22) that returns an empty list for any range, including ranges that match
    everything. It fails closed and silent — an empty result reads as "no spend" rather
    than as an error — so filtering happens client side.
    """
    import httpx

    r = httpx.get(
        f"{forge_url}/v1/usage",
        params={"account_id": account_id},
        headers={"Authorization": f"Bearer {admin_key}"},
        timeout=60,
    )
    r.raise_for_status()
    body = r.json()
    return body.get("data", body) if isinstance(body, dict) else body
