"""The IDE surface, tested against the live cluster with two real Keycloak users.

These run against k3s, not compose, and they are deliberately outside `tests/` — the
hermetic suite must stay provable with no cluster and no provider account.

    .venv-test/bin/pytest tests-live/test_workspace.py -v
    make test-workspace

Two things are being proved, and they fail in different directions:

  * The surface WORKS: a person logs in with their Keycloak account, gets a real terminal,
    runs real aider, and completes a real edit and a real build. Anything less than
    driving ttyd's websocket would pass while the product was broken — this repo has been
    burned by exactly that.
  * The surface is SAFE: the pod holds a key that spends real money and runs whatever code
    the user types, on a node shared with live GPU training. Every claim about what it
    cannot reach is checked by trying it from inside the user's own shell, not by reading
    the manifest.
"""

from __future__ import annotations

import json
import re
import subprocess

import pytest

from workspace_client import login, run_in_terminal, secret

NS = "enterprise-ai"
ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


def strip(text: str) -> str:
    return ANSI.sub("", text)


def kubectl(*args: str) -> str:
    return subprocess.run(
        ["kubectl", "-n", NS, *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def workspace_url(user: str) -> str:
    port = kubectl("get", "svc", f"ws-{user}", "-o", "jsonpath={.spec.ports[0].nodePort}")
    host = "192.168.2.44"  # k3s-worker; externalTrafficPolicy is Local, so this node only
    return f"http://{host}:{port}"


# ---------------------------------------------------------------- fixtures

@pytest.fixture(scope="session")
def users() -> list[dict]:
    """The two principals whose isolation is the point of the exercise."""
    return [
        {
            "name": secret("enterprise-ai-secrets", "BOOTSTRAP_USER"),
            "password": secret("enterprise-ai-secrets", "BOOTSTRAP_PASSWORD"),
        },
        {
            "name": secret("workspace-test-user", "USERNAME"),
            "password": secret("workspace-test-user", "PASSWORD"),
        },
    ]


@pytest.fixture(scope="session")
def sessions(users) -> dict:
    """One logged-in client per user, against that user's own workspace."""
    out = {}
    for u in users:
        url = workspace_url(u["name"])
        out[u["name"]] = {"url": url, "client": login(url, u["name"], u["password"])}
    return out


# ---------------------------------------------------------------- the front door

def test_workspace_is_not_reachable_without_logging_in(users):
    """No session, no terminal. Checked against the port, not the manifest."""
    import httpx

    for u in users:
        r = httpx.get(workspace_url(u["name"]) + "/", follow_redirects=False, timeout=30)
        assert r.status_code in (302, 303), (
            f"{u['name']}'s workspace answered {r.status_code} to an anonymous request"
        )
        assert "openid-connect/auth" in r.headers.get("location", ""), (
            f"anonymous request was not sent to the identity provider: {r.headers}"
        )
        assert "xterm" not in r.text.lower(), "the terminal was served without a login"

    # The websocket is the part that actually yields a shell; a proxy that guards the page
    # and forwards /ws would look fine in a browser and be completely open.
    for u in users:
        r = httpx.get(workspace_url(u["name"]) + "/ws", follow_redirects=False, timeout=30)
        assert r.status_code in (302, 303), f"/ws answered {r.status_code} anonymously"


def test_ttyd_is_published_nowhere(users):
    """ttyd has no authentication of its own. It must not be reachable, full stop."""
    published = subprocess.run(
        ["kubectl", "get", "svc", "-A", "-o", "json"],
        capture_output=True, text=True, check=True,
    ).stdout
    for svc in json.loads(published)["items"]:
        for port in svc["spec"].get("ports") or []:
            assert port.get("targetPort") != 7681 and port.get("port") != 7681, (
                f"{svc['metadata']['namespace']}/{svc['metadata']['name']} exposes ttyd"
            )

    import httpx

    with pytest.raises(httpx.HTTPError):
        httpx.get("http://192.168.2.44:7681/", timeout=8)


# ---------------------------------------------------------------- the surface works

def test_owner_gets_a_real_shell(sessions):
    for name, s in sessions.items():
        out = strip(run_in_terminal(s["url"], s["client"], ["whoami; pwd"], settle=4, timeout=90))
        assert "coder" in out, f"{name}: no shell output\n{out[-500:]}"
        assert "/workspace/project" in out, f"{name}: wrong working directory\n{out[-500:]}"


def test_a_build_command_runs(sessions):
    """`make test` — the outcome says the user must be able to build and run, not just chat."""
    for name, s in sessions.items():
        out = strip(run_in_terminal(
            s["url"], s["client"], ["make test; echo EXIT=$?"], settle=5, timeout=180))
        assert "EXIT=" in out, f"{name}: the build command never returned\n{out[-800:]}"
        assert "pytest" in out, f"{name}: make did not run pytest\n{out[-800:]}"


@pytest.mark.slow
def test_aider_completes_a_real_edit(sessions):
    """The whole point: real aider, in the browser, editing a real file against the gateway.

    Spends real money — a fraction of a cent per run on GLM through the gateway. That is
    deliberate: a workspace whose key cannot spend has not been tested.
    """
    for name, s in sessions.items():
        run_in_terminal(s["url"], s["client"], [
            "cd /workspace/project && git checkout -- . && "
            "printf 'def add(a, b):\\n    return a - b\\n\\n\\n"
            "def greet(name):\\n    return f\"hello, {name}\"\\n' > app.py && "
            "git commit -qam 'break add() again' && make test; echo BROKEN=$?",
        ], settle=5, timeout=120)

        out = strip(run_in_terminal(s["url"], s["client"], [
            "aider --yes-always app.py",
            "Fix the add function in app.py so that the tests pass.",
            "/exit",
            "make test; echo AFTER=$?",
        ], settle=15, timeout=900))

        assert "Applied edit" in out or "add(a, b)" in out, (
            f"{name}: aider produced no edit\n{out[-1500:]}"
        )
        assert "AFTER=0" in out, (
            f"{name}: the tests still fail after aider's edit\n{out[-1500:]}"
        )


# ---------------------------------------------------------------- the money

def test_each_pod_holds_only_its_own_ide_key(sessions):
    """Asked from inside the user's own shell, so it is the key the user actually has."""
    master = secret("enterprise-ai-secrets", "GATEWAY_MASTER_KEY")
    probe = (
        'curl -sS http://gateway:4000/key/info '
        '-H "Authorization: Bearer $OPENAI_API_KEY" '
        '| python -c "import sys,json; print(\'ALIAS=\'+str(json.load(sys.stdin)'
        '[\'info\'][\'key_alias\']))"'
    )
    for name, s in sessions.items():
        out = strip(run_in_terminal(s["url"], s["client"], [probe], settle=4, timeout=120))
        assert f"ALIAS={name}::ide" in out, (
            f"{name}'s pod is not holding {name}::ide\n{out[-800:]}"
        )

        leaked = strip(run_in_terminal(
            s["url"], s["client"],
            ['test "$OPENAI_API_KEY" = "%s" && echo MASTER || echo NOT_MASTER' % master],
            settle=3, timeout=90))
        assert "NOT_MASTER" in leaked, f"{name}'s pod holds the gateway master key"


def test_spend_is_attributed_to_the_right_principal(sessions):
    """The one bill: whatever the workspaces spent lands under `<user>/ide`.

    Read through the control plane rather than the gateway, because the claim being made
    is about the ledger the operator actually reads.
    """
    token = secret("enterprise-ai-secrets", "CONTROL_PLANE_ADMIN_TOKEN")
    raw = subprocess.run(
        ["kubectl", "-n", NS, "exec", "deploy/control-plane", "--",
         "python", "-c",
         "import httpx,sys;"
         "r=httpx.get('http://127.0.0.1:8000/admin/spend',"
         f"headers={{'Authorization':'Bearer {token}'}},timeout=60);"
         "print(r.text)"],
        capture_output=True, text=True, check=True,
    ).stdout
    spend = json.loads(raw)
    rows = {
        (r.get("username"), r.get("surface")): r
        for r in spend.get("by_user_and_surface", [])
    }
    for name in sessions:
        assert (name, "ide") in rows, (
            f"no ide-surface spend row for {name}; the surface is not being metered.\n"
            f"rows: {sorted(rows)}"
        )


# ---------------------------------------------------------------- the isolation

def test_one_user_cannot_open_another_users_workspace(users):
    """A full SSO round trip, not just a missing cookie.

    Checking only that the request 302s would prove nothing: the user already has a live
    Keycloak session, so a browser follows that redirect, gets a fresh code, and lands
    back. The authorization decision happens after that, and this test makes it happen.
    """
    a, b = users[0], users[1]
    client = login(workspace_url(a["name"]), a["name"], a["password"])
    assert client.get(workspace_url(a["name"]) + "/").status_code == 200

    intruding = client.get(workspace_url(b["name"]) + "/")  # follows redirects, as a browser does
    assert intruding.status_code == 403, (
        f"{a['name']} reached {b['name']}'s workspace with status "
        f"{intruding.status_code} at {intruding.url}"
    )
    assert "xterm" not in intruding.text.lower()


def test_users_cannot_read_each_others_files(sessions):
    names = list(sessions)
    for name in names:
        s = sessions[name]
        run_in_terminal(s["url"], s["client"],
                        [f"echo secret-of-{name} > /workspace/OWNER.txt"], settle=3, timeout=90)
    for name in names:
        s = sessions[name]
        out = strip(run_in_terminal(s["url"], s["client"],
                                    ["cat /workspace/OWNER.txt"], settle=3, timeout=90))
        assert f"secret-of-{name}" in out
        for other in names:
            if other != name:
                assert f"secret-of-{other}" not in out, (
                    f"{name} is reading {other}'s volume"
                )


def test_workspace_cannot_reach_the_cluster(sessions):
    """Tried from inside the user's shell. Every one of these must fail.

    The gateway is the single exception, and it is checked too — an isolation test that
    passes because the pod has no network at all is not evidence of anything.
    """
    name, s = next(iter(sessions.items()))
    other = [n for n in sessions if n != name][0]
    other_ip = kubectl("get", "pod", "-l", f"workspace.enterprise-ai/user={other}",
                       "-o", "jsonpath={.items[0].status.podIP}")

    blocked = {
        "kubernetes API (service)": "curl -sSk -m 6 https://10.43.0.1/version",
        "kubernetes API (node)": "curl -sSk -m 6 https://192.168.2.43:6443/version",
        "control plane": "curl -sS -m 6 http://control-plane:8000/health",
        "the other workspace's proxy": f"curl -sS -m 6 http://{other_ip}:4180/ping",
        "the other workspace's ttyd": f"curl -sS -m 6 http://{other_ip}:7681/",
    }
    script = "; ".join(
        f'{cmd} >/dev/null 2>&1 && echo "REACHED::{label}" || echo "blocked::{label}"'
        for label, cmd in blocked.items()
    )
    out = strip(run_in_terminal(s["url"], s["client"], [script], settle=5, timeout=240))
    for label in blocked:
        assert f"REACHED::{label}" not in out, f"{name}'s workspace can reach {label}\n{out[-900:]}"
        assert f"blocked::{label}" in out, f"probe for {label} did not run\n{out[-900:]}"

    reachable = strip(run_in_terminal(s["url"], s["client"], [
        'curl -sS -m 15 -o /dev/null -w "GATEWAY=%{http_code}\\n" '
        'http://gateway:4000/health/liveliness'
    ], settle=5, timeout=120))
    assert "GATEWAY=200" in reachable, f"the gateway is not reachable either\n{reachable[-500:]}"


def test_workspace_has_no_service_account_token(sessions):
    name, s = next(iter(sessions.items()))
    out = strip(run_in_terminal(
        s["url"], s["client"],
        ["ls /var/run/secrets/kubernetes.io/serviceaccount 2>&1 || echo NO_TOKEN"],
        settle=3, timeout=90))
    assert "NO_TOKEN" in out or "No such file" in out, f"a service-account token is mounted\n{out}"


def test_workspace_pods_are_bounded(users):
    """k3s-worker runs live GPU training. Requests AND limits, on every container."""
    for u in users:
        spec = json.loads(kubectl("get", "deployment", f"ws-{u['name']}", "-o", "json"))
        containers = spec["spec"]["template"]["spec"]["containers"]
        assert len(containers) == 2
        for c in containers:
            res = c.get("resources") or {}
            assert res.get("requests", {}).get("cpu"), f"{c['name']}: no cpu request"
            assert res.get("requests", {}).get("memory"), f"{c['name']}: no memory request"
            assert res.get("limits", {}).get("cpu"), f"{c['name']}: no cpu limit"
            assert res.get("limits", {}).get("memory"), f"{c['name']}: no memory limit"

        pod = spec["spec"]["template"]["spec"]
        assert pod.get("automountServiceAccountToken") is False
        assert pod["serviceAccountName"] == "workspace"
        assert pod["securityContext"]["runAsNonRoot"] is True
        for c in containers:
            assert c["securityContext"]["allowPrivilegeEscalation"] is False
            assert c["securityContext"]["capabilities"]["drop"] == ["ALL"]
