"""The only place a live test may resolve a human identity, and the gate on doing it.

WHY THIS EXISTS

Every browser and workspace suite in this directory used to open a session like this:

    _secret("workspace-user-student", "USERNAME"), _secret("workspace-user-student", "PASSWORD")

`student` is a PERSON. Signing in as them is not a read: the Code tab drives that person's
own pod, so switching to it starts a session, rewrites `.meta/<project>.session` and
`rm -f`s the `.new-session` flag. A measured run on the previous attempt changed both. A
test that can silently convert somebody's session cannot be allowed to resolve their
credential by default, and a comment saying "do not run this while anyone is signed in" is
not a guard — this item exists because that warning was already there and a run happened
anyway.

WHAT REPLACED IT

Most of those tests did not need a person at all; they needed A portal and A workshop.
Those are hosted on loopback now (portal_harness.py) and take no identity from anywhere.
What is left here is the genuine residue: tests whose subject IS the live deployment — a
real Keycloak login, a real pod, a real gateway key, real money. For those, two gates must
BOTH be open, and neither has a default that reaches a person:

  1. `EAI_LIVE_TEST_USER` names the account. There is no default. Unset means every test
     that needs one is deselected before it runs (see conftest.py), so no invocation of
     pytest — targeted, whole-directory, or by accident — can reach a credential.

  2. That account's Secret must carry `THROWAWAY`. `deploy/bin/ensure-second-user.sh
     --throwaway <name>` writes it; a person's account does not have it. So pointing gate 1
     at `student` still fails, which is the point: the env var alone would make the hazard
     one impatient command away, and impatience is how the last run happened.

There is deliberately no override. Marking somebody's live account throwaway means writing
to their Secret, which is a decision with a name attached rather than a flag on a test run.
"""

from __future__ import annotations

import base64
import os
import subprocess

import pytest

NS = "enterprise-ai"

#: The env var that names the throwaway account. No default, ever.
ENV_USER = "EAI_LIVE_TEST_USER"

#: The env var that permits tests which write cluster state a live user depends on.
ENV_MAINTENANCE = "EAI_LIVE_MAINTENANCE_WINDOW"

#: A second throwaway account. Needed by the isolation suite, whose whole claim is about a
#: pair — one principal proves nothing about what the other cannot reach.
ENV_USER_2 = "EAI_LIVE_TEST_USER_2"

#: The env var that names an OPERATOR account, for the read-only operator-console tests.
#: Separate from ENV_USER because an operator is a different thing: membership of
#: PORTAL_ADMINS, which a throwaway account does not have and cannot give itself.
ENV_OPERATOR = "EAI_LIVE_TEST_OPERATOR"

#: Applied to any test that signs in as, or drives the pod of, an account on the live
#: deployment. Gated on ENV_USER.
MARK_REAL_USER = "needs_real_user"

#: Applied to any test that writes deployment-wide cluster state (a shared ConfigMap, a
#: deployment restart, a provisioning run). Gated on ENV_MAINTENANCE.
MARK_MUTATES = "mutates_live_deployment"

_PROVISION_HINT = (
    "Provision a throwaway account first, in a maintenance window (both commands write to\n"
    "the cluster):\n"
    "    deploy/bin/ensure-second-user.sh --throwaway eaibot\n"
    "    deploy/bin/provision-workspace.sh eaibot\n"
    "then run the suite with EAI_LIVE_TEST_USER=eaibot."
)


def requested_user() -> str:
    """The account name the operator explicitly named, or "" if they named none."""
    return os.environ.get(ENV_USER, "").strip()


def maintenance_window() -> bool:
    return os.environ.get(ENV_MAINTENANCE, "").strip() not in ("", "0", "false", "no")


def _secret_field(name: str, key: str) -> str:
    """One field of one namespace Secret, or "" if it is absent.

    Absent and empty are deliberately the same answer here: the only callers are the two
    gate checks and the credential read, and all three treat "not there" as "refuse".
    """
    r = subprocess.run(
        ["kubectl", "-n", NS, "get", "secret", name, "-o", f"jsonpath={{.data.{key}}}"],
        capture_output=True, text=True, timeout=60,
    )
    raw = r.stdout.strip()
    if r.returncode != 0 or not raw:
        return ""
    try:
        return base64.b64decode(raw).decode()
    except Exception:
        return ""


#: Every credential this module has handed out, in order, as "<env>=<account>". Names only —
#: no password is ever recorded here. Kept for the failure messages.
served: list[str] = []

#: The names of the fixtures that have resolved a live identity this session.
#:
#: This, not `served`, is what conftest.py's backstop keys on, and the difference is the
#: whole point. A session-scoped credential fixture is set up ONCE, so `served` grows once —
#: during the first (marked) test. An unmarked test that depends on the same fixture gets the
#: cached value, `served` does not move, and a hook watching it sees nothing. MEASURED: the
#: first version of the backstop watched `served` and the reuse probe passed straight through
#: it. What survives caching is the fixture CLOSURE: `item.fixturenames` names the fixture for
#: every consumer, cached or not.
#:
#: Keyed on the bare fixture name, so a same-named fixture elsewhere in tests-live that does
#: NOT resolve an identity would be flagged too. That is the safe direction to be wrong in —
#: it fails loudly with the fixture named, and the fix is a rename — and it is preferred to
#: keying on (file, name), which would MISS a conftest fixture shared across files.
credential_fixtures: set[str] = set()


def _requesting_item(request):
    """The test function a fixture is being set up FOR, whatever the fixture's scope.

    `request.node` is NOT that. For a session-scoped fixture it is the Session and for a
    module-scoped one the Module, and neither carries the test's markers — so the obvious
    `request.node.get_closest_marker(...)` returns None for a perfectly well-marked test and
    the guard refuses everything. MEASURED: the first version of this did exactly that, and
    it fails CLOSED, which is the dangerous kind of wrong — the marked suites become
    permanently unrunnable, and the pressure then goes on deleting the guard.

    `request._pyfuncitem` is the triggering item and is correct at every scope (verified
    against pytest 8.4.2 with a session-scoped fixture). It is private, so this falls back to
    `request.node` and returns None when neither is an item; a None result makes the check
    below abstain rather than refuse, and conftest.py's hook is what closes the gap. Two
    mechanisms, because a single one that can be silently wrong is what this repo keeps
    shipping.
    """
    item = getattr(request, "_pyfuncitem", None)
    if item is None:
        item = getattr(request, "node", None)
    return item if hasattr(item, "get_closest_marker") and hasattr(item, "nodeid") else None


def _require_marker(request, marker: str = MARK_REAL_USER) -> None:
    """Refuse to hand a credential to a test that is not marked.

    THIS IS THE GATE'S OTHER HALF, and it is here rather than in a lint because the two
    halves have to agree or neither works.

    conftest.py deselects marked tests when no account is named. That closes one direction:
    a marked test cannot run by accident. It does nothing about the other — a NEW test, or a
    new fixture, that reaches a credential without carrying the marker is not deselected by
    anything and runs against a live account on a bare `pytest tests-live/`.

    Checking marker coverage from outside is guesswork: a test can be marked by `pytestmark`,
    by a decorator, or through a parametrisation, and a static reading has to reimplement all
    three. So the credential function asks instead. An unmarked caller fails here, before any
    kubectl runs, which means the failure is free and cannot leak a name.
    """
    # Register the fixture BEFORE deciding, so the backstop covers every later consumer even
    # if this call is the one that refuses.
    name = getattr(request, "fixturename", None)
    if name:
        credential_fixtures.add(name)

    item = _requesting_item(request)
    if item is None:
        # Abstain rather than refuse: see _requesting_item. The hook still fails the test.
        return
    if item.get_closest_marker(marker) is None:
        pytest.fail(
            f"{item.nodeid} asked for a live account but is not marked `{marker}`.\n\n"
            "That marker is what makes conftest.py hold the test back when no throwaway "
            "account is named. Without it the test would sign in to the live deployment on "
            "any plain `pytest tests-live/` run — which is how a real person's workspace "
            "session came to be mutated by a test (enterpriseaiframework-cf5).\n\n"
            f"Add `@pytest.mark.{marker}` to the test, or "
            f"`pytestmark = pytest.mark.{marker}` to the module."
        )


def account(request) -> tuple[str, str]:
    """(username, password) for the throwaway account, or a loud failure.

    Never returns a credential that was not both named by the operator and marked
    throwaway by whoever created it — and never hands one to an unmarked test.

    `request` is not optional. Making it so would let a caller opt out of the marker check by
    forgetting an argument, which is the same shape of mistake as the docstring warning that
    did not stop the run this item exists because of.
    """
    _require_marker(request)
    return _account_from(ENV_USER)


def second_account(request) -> tuple[str, str]:
    """A second throwaway account, for the claims that need a pair.

    Same two gates, its own env var. Deliberately NOT defaulted to anything — deriving a
    second principal (`<first>2`, the other name in some Secret) is exactly how the suite
    ended up pointed at two real people without anybody deciding to.
    """
    _require_marker(request)
    return _account_from(ENV_USER_2)


def _account_from(env: str) -> tuple[str, str]:
    name = os.environ.get(env, "").strip()
    if not name:
        pytest.fail(
            f"{env} is not set, so there is no account this test may sign in as.\n\n"
            "It used to read secret/workspace-user-student — a real person, whose workspace "
            "pod these tests then drive.\n\n" + _PROVISION_HINT
        )

    secret_name = f"workspace-user-{name}"
    if not _secret_field(secret_name, "THROWAWAY"):
        pytest.fail(
            f"{env}={name!r} but secret/{secret_name} is not marked THROWAWAY, so as "
            f"far as this suite can tell {name!r} is a person.\n\n"
            "These tests do not merely read: they start a session in that account's own "
            "workspace pod, which rewrites its session bookkeeping. Refusing.\n\n"
            + _PROVISION_HINT
        )

    username = _secret_field(secret_name, "USERNAME")
    password = _secret_field(secret_name, "PASSWORD")
    if not username or not password:
        pytest.fail(
            f"secret/{secret_name} is marked THROWAWAY but has no USERNAME/PASSWORD; "
            f"re-run deploy/bin/ensure-second-user.sh --throwaway {name}"
        )
    # The name in the Secret wins over the env var. They are the same today, but the
    # Secret is what the realm was told and the env var is what somebody typed.
    served.append(f"{env}={username}")
    return username, password


def pod_owner() -> str:
    """The username whose workspace pod a marked test is allowed to drive."""
    return account()[0]


def operator_account(request) -> tuple[str, str]:
    """(username, password) for an account in PORTAL_ADMINS, or a loud failure.

    THIS ONE CANNOT BE MADE SAFE THE WAY `account()` WAS, and saying so is the point.

    An operator console cannot be exercised without an operator, and being an operator means
    being named in the control plane's PORTAL_ADMINS — which a throwaway account is not, and
    cannot become without an operator editing deployment configuration. The only operator on
    the deployment today is a person.

    So this refuses to guess. It used to read BOOTSTRAP_USER/BOOTSTRAP_PASSWORD out of
    secret/enterprise-ai-secrets, which is a login as the founder performed by a test run
    with nobody's say-so. Now the operator must be named explicitly, every time, by whoever
    is running the suite — and the tests that need it are deselected until then.

    Making these runnable without a person's credential needs PORTAL_ADMINS to include a
    throwaway account. That is a control-plane configuration change, not something a test
    can arrange, and it is filed rather than smuggled in here.
    """
    _require_marker(request)
    name = os.environ.get(ENV_OPERATOR, "").strip()
    if not name:
        pytest.fail(
            f"{ENV_OPERATOR} is not set. These tests sign in as an OPERATOR — a member of "
            "PORTAL_ADMINS — and the only operator on this deployment is a person.\n\n"
            "This used to read BOOTSTRAP_USER/BOOTSTRAP_PASSWORD from "
            "secret/enterprise-ai-secrets without anybody being asked. If you mean to sign "
            f"in as the operator, name them: {ENV_OPERATOR}=<username>. Their password is "
            "read from secret/enterprise-ai-secrets only when their name matches "
            "BOOTSTRAP_USER; any other operator must be given a Secret of their own."
        )
    bootstrap = _secret_field("enterprise-ai-secrets", "BOOTSTRAP_USER")
    if name == bootstrap:
        password = _secret_field("enterprise-ai-secrets", "BOOTSTRAP_PASSWORD")
    else:
        password = _secret_field(f"workspace-user-{name}", "PASSWORD")
    if not password:
        pytest.fail(
            f"{ENV_OPERATOR}={name!r} but no password is available for them; expected "
            f"BOOTSTRAP_PASSWORD in secret/enterprise-ai-secrets or PASSWORD in "
            f"secret/workspace-user-{name}"
        )
    served.append(f"{ENV_OPERATOR}={name}")
    return name, password


def public_base_url() -> str:
    """The deployment's own public origin.

    Not a credential and not an identity — every user of the deployment shares it — so it
    is read straight out of the deployment Secret with no gate. It lives here so that the
    suites which need it do not each keep a `kubectl get secret` helper, which is how the
    credential read got copied into four files in the first place.
    """
    url = _secret_field("enterprise-ai-secrets", "PUBLIC_BASE_URL").rstrip("/")
    if not url:
        pytest.fail(
            "could not read PUBLIC_BASE_URL from secret/enterprise-ai-secrets; is kubectl "
            f"pointed at the {NS} namespace?"
        )
    return url


def deployment_secret(name: str, key: str) -> str:
    """A namespace Secret field that is NOT a person's login credential.

    Gateway master keys, per-user issued API keys, the control plane's admin token: these
    are deployment secrets, and reading one does not put a session at risk the way signing
    in as somebody does. Kept distinct from `account()` on purpose — the gate exists to stop
    tests ACTING AS a person, not to stop them reading configuration — and kept in this
    module so that `tests/test_live_suite_identity.py` has one place to allow.
    """
    value = _secret_field(name, key)
    if not value:
        pytest.fail(f"secret/{name} has no usable {key} in namespace {NS}")
    return value
