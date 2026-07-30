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

WHOSE SHELLS THESE ARE (enterpriseaiframework-cf5)

There is no hermetic version of this file and there should not be. Every claim in it is
about the cluster: a NodePort, an oauth2-proxy allow-list rendered per user, a CNI that
resolves packets on the destination's ingress rules, a service-account token that must not
be mounted, a pod's own issued key. A loopback stand-in would answer whatever it was written
to answer, which is the failure mode the module docstring above already warns about twice.

What was wrong is WHOSE shells it typed into. `users` resolved to BOOTSTRAP_USER out of
secret/enterprise-ai-secrets — the founder — and to secret/workspace-test-user, which names
`student`. Both are people, and the tests below do not merely look: they run `make test` in
those shells, delete `.pytest_cache`, write `/workspace/OWNER.txt`, and let aider rewrite
`app.py` and commit it. That is somebody's working directory.

So the module is marked `needs_real_user` and both principals come from live_identity.py,
which refuses anything not explicitly named AND marked THROWAWAY. Two principals are
required — one user proves nothing about isolation — so the suite now needs two throwaway
accounts rather than borrowing two real ones; `EAI_LIVE_TEST_USER` names the first and
`EAI_LIVE_TEST_USER_2` the second. conftest.py deselects the module until they are named.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
import uuid

import pytest

import live_identity
from workspace_client import login, run_in_terminal, secret

# Every test in here logs in as, or reads the pod of, a workspace account. The ones that
# type into a shell are the reason this is at module scope rather than per test.
pytestmark = pytest.mark.needs_real_user

NS = "enterprise-ai"
ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")


def strip(text: str) -> str:
    return ANSI.sub("", text)


def probe(session: dict, script: str, marker_file: str = "/tmp/probe.out",
          settle: float = 5.0, timeout: float = 240.0) -> str:
    """Run `script` in the shell, then read its result back in a SECOND round trip.

    A terminal echoes what you type. Any assertion made against the transcript of the
    command that produced the answer is satisfied by the echo of the command itself —
    `make test; echo EXIT=$?` puts "EXIT=" on screen whether or not make ever ran. So the
    script writes its answer to a file, and the answer is read with `cat`, whose own echo
    contains only the path. Every probe in this module goes through here for that reason.
    """
    run_in_terminal(session["url"], session["client"],
                    [f"rm -f {marker_file}; " + script], settle=settle, timeout=timeout)
    return strip(run_in_terminal(session["url"], session["client"], [f"cat {marker_file}"],
                                 settle=4, timeout=90))


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
def users(request) -> list[dict]:
    """The two principals whose isolation is the point of the exercise.

    Two, not one, and that is not negotiable: the refusal being tested is made by the
    DESTINATION workspace's own oauth2-proxy against its own allow-list, and those manifests
    are rendered per user, so a one-directional probe cannot see a per-user rendering error.

    Both are throwaway, and both must be named. The pair used to be the founder and
    `student`; see the module docstring.
    """
    first, second = live_identity.account(request), live_identity.second_account(request)
    assert first[0] != second[0], (
        f"both principals resolved to {first[0]!r}; the isolation claim would be vacuous — "
        f"set {live_identity.ENV_USER_2} to a DIFFERENT throwaway account"
    )
    return [
        {"name": first[0], "password": first[1]},
        {"name": second[0], "password": second[1]},
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
    """`make test` — the outcome says the user must be able to build and run, not just chat.

    Asserted on the build's effect on disk, not on the transcript. The first cut of this
    test typed `make test; echo EXIT=$?` and asserted "EXIT=" appeared: the terminal
    echoes the line you type, so that substring was on screen before make had done
    anything at all, and the test would have passed against a pod with no make, no
    pytest, and no project. The evidence used instead is pytest's own cache — deleted
    first, so its reappearance carrying this project's node ids means pytest collected and
    ran these tests during THIS command — plus the recorded exit status and the run
    summary, read back in a second round trip.

    Pass or fail is deliberately not asserted: the seeded `add()` is wrong on purpose and
    the aider test leaves the file in whichever state it finished in. What is being proved
    is that the toolchain runs, so the assertion is that make reported a pytest verdict
    (0 or 1) rather than falling over (2 = no such target, 127 = no such command).
    """
    for name, s in sessions.items():
        out = probe(s, (
            "cd /workspace/project && rm -rf .pytest_cache /tmp/build.log && "
            "{ make test >/tmp/build.log 2>&1; echo rc=$? >/tmp/build.out; } && "
            "cat /tmp/build.log >> /tmp/build.out && "
            "cat .pytest_cache/v/cache/nodeids >> /tmp/build.out 2>&1"
        ), marker_file="/tmp/build.out", settle=6, timeout=240)

        rc = re.search(r"rc=(\d+)", out)
        assert rc, f"{name}: the build command never recorded an exit status\n{out[-800:]}"
        assert rc.group(1) in ("0", "1"), (
            f"{name}: `make test` exited {rc.group(1)} — the toolchain did not run "
            f"(2 = no such make target, 127 = command not found)\n{out[-800:]}"
        )
        assert re.search(r"\d+ (passed|failed)", out), (
            f"{name}: no pytest run summary — make did not actually run the tests\n{out[-800:]}"
        )
        assert "test_app.py::test_add" in out, (
            f"{name}: pytest left no fresh collection cache, so it did not collect and run "
            f"this project's tests in that command\n{out[-800:]}"
        )


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

        transcript = strip(run_in_terminal(s["url"], s["client"], [
            "aider --yes-always app.py",
            "Fix the add function in app.py so that the tests pass.",
            "/exit",
        ], settle=15, timeout=900))

        # The claim is that the file on disk changed and the project now builds — not
        # that certain words appeared on screen. `"add(a, b)" in transcript` was standing
        # in for the first half and is satisfied by aider merely quoting the unmodified
        # file back, which is exactly what it does when an edit silently fails to apply
        # (dogfood-findings.md records that failure mode on this surface).
        out = probe(s, (
            "cd /workspace/project && "
            "{ make test >/tmp/after.log 2>&1; echo AFTER=$? >/tmp/after.out; } && "
            'printf "SUM=%s\\n" "$(make run 2>&1 | tail -1)" >> /tmp/after.out'
        ), marker_file="/tmp/after.out", settle=6, timeout=240)

        assert "SUM=5" in out, (
            f"{name}: add() still does not add after aider's edit — the file on disk was "
            f"not fixed\n{out[-600:]}\naider said:\n{transcript[-1500:]}"
        )
        assert "AFTER=0" in out, (
            f"{name}: the tests still fail after aider's edit\n{out[-600:]}"
        )


# ---------------------------------------------------------------- the money

def test_each_pod_holds_only_its_own_ide_key(sessions):
    """Asked from inside the user's own shell, so it is the key the user actually has."""
    master_digest = hashlib.sha256(
        secret("enterprise-ai-secrets", "GATEWAY_MASTER_KEY").encode()
    ).hexdigest()

    ask_alias = (
        'curl -sS http://gateway:4000/key/info '
        '-H "Authorization: Bearer $OPENAI_API_KEY" '
        '| python -c "import sys,json; print(\'ALIAS=\'+str(json.load(sys.stdin)'
        '[\'info\'][\'key_alias\']))" > /tmp/keyinfo.out 2>&1'
    )
    for name, s in sessions.items():
        out = probe(s, ask_alias, marker_file="/tmp/keyinfo.out", settle=4, timeout=120)
        assert f"ALIAS={name}::ide" in out, (
            f"{name}'s pod is not holding {name}::ide\n{out[-800:]}"
        )

        # The comparison is made here, not in the pod, for two reasons. The master key
        # must not be typed into a shell the user owns — the earlier version of this test
        # put it in the pod's transcript and shell history to compare it, which is a real
        # leak in a test that exists to prove there is no leak. And a pod-side
        # `... && echo MASTER || echo NOT_MASTER` is satisfied by the terminal echoing the
        # line that contains both words, so it passed no matter what the key was.
        digest = probe(
            s, 'printf %s "$OPENAI_API_KEY" | sha256sum | cut -d" " -f1 > /tmp/keyhash.out',
            marker_file="/tmp/keyhash.out", settle=3, timeout=90)
        found = re.search(r"\b([0-9a-f]{64})\b", digest)
        assert found, f"{name}: could not read the pod's key digest\n{digest[-400:]}"

        # Positive control first. An inequality on its own is the weakest possible
        # assertion: a stray newline anywhere in the pipeline makes every digest differ
        # from every other, and the test would report "not the master key" while
        # measuring nothing. Pinning the digest to the key this user was actually issued
        # proves the comparison is capable of being equal before it is trusted to be
        # unequal.
        own = hashlib.sha256(secret(f"ws-{name}-key", "OPENAI_API_KEY").encode()).hexdigest()
        assert found.group(1) == own, (
            f"{name}'s pod is not holding the key the control plane issued it"
        )
        assert found.group(1) != master_digest, f"{name}'s pod holds the gateway master key"


def spend_rows() -> list[dict]:
    """The bill as the operator reads it — through the control plane, not the gateway."""
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
    return json.loads(raw).get("by_user_and_surface", [])


def test_the_ide_surface_is_metered(sessions):
    """Every workspace's traffic reaches the one bill under its own `<user>/ide` row.

    This is a presence check and it is labelled as one. It proves the surface is metered
    at all; it does NOT prove the row's principal is the key's owner. That stronger claim
    is the next test, and it currently fails.
    """
    rows = {(r.get("username"), r.get("surface")) for r in spend_rows()}
    for name in sessions:
        assert (name, "ide") in rows, (
            f"no ide-surface spend row for {name}; the surface is not being metered.\n"
            f"rows: {sorted(rows)}"
        )


@pytest.mark.xfail(
    strict=True,
    reason="enterpriseaiframework-522: metering.py ranks the caller-supplied end_user "
           "above the key alias, so a workspace can bill its spend to any name it likes",
)
def test_spend_is_attributed_to_the_key_that_paid_for_it(sessions):
    """The bill names the principal who spent the money, not a name the caller chose.

    "There is an ide row for this user" was standing in for this claim and is not it: the
    row can exist while the money lands somewhere else. The gap is real and was
    demonstrated — a workspace pod, using nothing but its own issued key, puts an
    arbitrary string in the bill by sending `{"user": "..."}` in the request body, because
    `metering.py` prefers `end_user` over the alias the key carries. One user can
    therefore charge their spend to another user, or to a name that belongs to nobody.

    The defect is filed as enterpriseaiframework-522 and is not fixed here. This test is
    written to the real claim and marked strict-xfail so the suite records the truth
    instead of passing over it — when 522 lands, this turns green and the strict marker
    fails the run until the marker is removed.
    """
    name, s = next(iter(sessions.items()))
    sentinel = f"not-a-principal-{uuid.uuid4().hex[:10]}"

    before = sum(
        r["requests"] for r in spend_rows()
        if r.get("username") == name and r.get("surface") == "ide"
    )

    # fake-large is the in-cluster provider: this must not depend on an upstream account,
    # and the claim under test is about attribution, not about who serves the tokens.
    call = (
        'curl -sS -o /tmp/attr.body -w "HTTP=%{http_code}\\n" '
        'http://gateway:4000/v1/chat/completions '
        '-H "Authorization: Bearer $OPENAI_API_KEY" '
        '-H "Content-Type: application/json" '
        '-d \'{"model":"fake-large","messages":[{"role":"user","content":"bill me"}],'
        f'"user":"{sentinel}"}}\' > /tmp/attr.out 2>&1'
    )
    result = probe(s, call, marker_file="/tmp/attr.out", settle=5, timeout=180)
    assert "HTTP=200" in result, (
        f"{name}: the metered request never completed, so this proves nothing about "
        f"attribution\n{result[-600:]}"
    )

    # Wait for the ledger to catch up, then read it once. Asserting before the row lands
    # would make a strict-xfail test pass for the wrong reason.
    deadline = time.monotonic() + 90
    rows: list[dict] = []
    while time.monotonic() < deadline:
        rows = spend_rows()
        landed = sum(
            r["requests"] for r in rows
            if r.get("username") == name and r.get("surface") == "ide"
        )
        if landed > before or any(r.get("username") == sentinel for r in rows):
            break
        time.sleep(3)
    else:
        pytest.fail("the request never reached the bill under any name")

    assert sentinel not in {r.get("username") for r in rows}, (
        f"a workspace billed its spend to '{sentinel}', a principal that does not exist. "
        f"The row must carry the owner of the key that paid — {name}."
    )


# ---------------------------------------------------------------- the isolation

def test_one_user_cannot_open_another_users_workspace(users):
    """A full SSO round trip, in BOTH directions.

    Checking only that the request 302s would prove nothing: the user already has a live
    Keycloak session, so a browser follows that redirect, gets a fresh code, and lands
    back. The authorization decision happens after that, and this test makes it happen.

    Both directions, because the refusal is made by the *destination* workspace's own
    oauth2-proxy against its own allow-list, and those manifests are rendered per user.
    Proving a → b is refused says only that b's allow-list is right; a stray
    `--email-domain=*` on a's proxy would leave a's workspace open to every account in
    the realm and a one-directional test would still be green. The pair is the claim.

    /ws is probed as well as /. The page is not the asset — the websocket is the thing
    that yields a shell, and a proxy that guards the page while forwarding the upgrade
    would look correct in a browser and be completely open.
    """
    for me, them in ((users[0], users[1]), (users[1], users[0])):
        mine, theirs = workspace_url(me["name"]), workspace_url(them["name"])
        client = login(mine, me["name"], me["password"])
        try:
            own = client.get(mine + "/")
            # Establishes that the login actually took. Without this, a client whose SSO
            # exchange silently failed would be refused everywhere and the test below
            # would pass while proving nothing about authorization.
            assert own.status_code == 200, (
                f"{me['name']} could not open their own workspace: {own.status_code}"
            )
            assert "ttyd - terminal" in own.text.lower(), (
                f"{me['name']} did not land on their terminal; got {own.url}"
            )

            for path in ("/", "/ws"):
                # follow_redirects is on, as in a browser: the intruder's live Keycloak
                # session satisfies the redirect and the proxy then has to decide.
                intruding = client.get(theirs + path)
                assert intruding.status_code == 403, (
                    f"{me['name']} reached {them['name']}'s workspace{path} with status "
                    f"{intruding.status_code} at {intruding.url}"
                )
                assert "ttyd" not in intruding.text.lower(), (
                    f"{me['name']} was served {them['name']}'s terminal"
                )
        finally:
            client.close()


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
    """Tried from inside EVERY user's shell. Every one of these must fail.

    From both pods, not one. The NetworkPolicy is symmetric by construction today, but
    whether a packet arrives is decided by the *destination* pod's ingress rules and by
    its Service's externalTrafficPolicy, and both of those are rendered per user from a
    template. A one-sided probe cannot see a per-user rendering error — it would report
    the same "blocked" whether the other side is fenced or wide open, because the leg it
    tests is the one that happens to be correct.

    The gateway is the single exception, and it is checked too — an isolation test that
    passes because the pod has no network at all is not evidence of anything.
    """
    for name, s in sessions.items():
        other = [n for n in sessions if n != name][0]
        other_ip = kubectl("get", "pod", "-l", f"workspace.enterprise-ai/user={other}",
                           "-o", "jsonpath={.items[0].status.podIP}")
        assert other_ip, f"could not resolve {other}'s pod IP; the probe would be vacuous"

        blocked = {
            "kubernetes API (service)": "curl -sSk -m 6 https://10.43.0.1/version",
            "kubernetes API (node)": "curl -sSk -m 6 https://192.168.2.43:6443/version",
            "control plane": "curl -sS -m 6 http://control-plane:8000/health",
            f"{other}'s proxy": f"curl -sS -m 6 http://{other_ip}:4180/ping",
            f"{other}'s ttyd": f"curl -sS -m 6 http://{other_ip}:7681/",
        }
        script = "; ".join(
            f'{cmd} >/dev/null 2>&1 && echo "REACHED::{label}" >> /tmp/netprobe.out '
            f'|| echo "blocked::{label}" >> /tmp/netprobe.out'
            for label, cmd in blocked.items()
        )
        out = probe(s, script, marker_file="/tmp/netprobe.out", settle=5, timeout=240)
        for label in blocked:
            assert f"REACHED::{label}" not in out, (
                f"{name}'s workspace can reach {label}\n{out[-900:]}"
            )
            assert f"blocked::{label}" in out, f"probe for {label} did not run\n{out[-900:]}"

        reachable = probe(s, (
            'curl -sS -m 15 -o /dev/null -w "GATEWAY=%{http_code}\\n" '
            'http://gateway:4000/health/liveliness > /tmp/gw.out 2>&1'
        ), marker_file="/tmp/gw.out", settle=5, timeout=120)
        assert "GATEWAY=200" in reachable, (
            f"{name}'s workspace cannot reach the gateway either, so the result above is "
            f"not evidence of a fence\n{reachable[-500:]}"
        )


def test_workspace_has_no_service_account_token(sessions):
    """Every pod, and read back through a file.

    `ls ... || echo NO_TOKEN` asserted against the transcript passes on the echo of the
    line that contains "NO_TOKEN" — even with a token mounted and listed. The listing
    itself is what gets asserted on here, and a mounted token is a per-user manifest
    error, so both pods are asked.
    """
    for name, s in sessions.items():
        out = probe(
            s,
            "ls -A /var/run/secrets/kubernetes.io/serviceaccount > /tmp/sa.out 2>&1; "
            "cat /var/run/secrets/kubernetes.io/serviceaccount/token >> /tmp/sa.out 2>&1",
            marker_file="/tmp/sa.out", settle=3, timeout=90)
        assert "No such file" in out, (
            f"{name}: a service-account token directory is mounted\n{out[-500:]}"
        )
        assert "eyJ" not in out, f"{name}: a service-account token is readable\n{out[-300:]}"


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
