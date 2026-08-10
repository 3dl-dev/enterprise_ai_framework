"""Attaching a console to a REAL resident agent, and proving it attached.

Run against the live k3s cluster:  pytest tests-live/test_agent_console.py

This is `enterpriseaiframework-0e7` measured rather than asserted. The hermetic suite
(control-plane/tests/test_agent_console.py) proves the authorisation boundary and the
transfer semantics against a fake daemon. It cannot prove the one claim the surface is
FOR: that `/agents/<name>/` reaches a process which was already running, keeps running
when the console goes away, and is still the SAME PROCESS when the console comes back.
That claim is about a pid in a pod, so it is measured on a pod.

WHY THIS RUNS THE APP ITSELF INSTEAD OF CALLING THE DEPLOYED CONTROL PLANE

The live control plane is running an older image and it is fragile right now — the
external OIDC front door is flapping and the pod crashloops when restarted, with a camp
tomorrow. Rolling it to test a new route would be trading the login everybody needs for a
console nobody has yet. So NOTHING here touches the `control-plane` Deployment. The NEW
app is run in this process, on loopback, against:

  * the REAL Kubernetes API, through `kubectl proxy`, so `agents.console_target` reads the
    real Deployment's owner label and the real Secret's console password — the two things
    the whole authorisation rests on;
  * the REAL agent pod, through `kubectl port-forward` on the Service's own port 4096.

Only two things are substituted, and both are the CLUSTER rather than the code under test:
the API server's ADDRESS (which a pod gets from its environment) and the resolution of the
Service NAME (which a pod gets from cluster DNS). The port, the protocol, the credential,
the proxy and the identity predicate are all the shipped ones — identity in particular is
injected as the header oauth2-proxy sets, over the loopback hop `require_user` demands, so
no endpoint here is handed a name it did not authenticate.

WHERE THE EXPECTED VALUES COME FROM

Not from `app/agent_console.py`, in any of the claims that matter:

  * the resident pid and the container restart count are read with `kubectl exec` /
    `kubectl get pod`, not inferred from a response;
  * "the daemon demands a credential" is measured against the pod directly on the
    forwarded port, where an unauthenticated request must be 401 — so the 200 through the
    proxy is evidence the proxy supplied something, not evidence the daemon is open;
  * the entry document's asset references are compared against what the REAL daemon
    serves, read from the same pod on the same connection;
  * the cross-user refusal is driven by a SECOND REAL IDENTITY through the same loopback
    hop, and the proof it was refused is that the daemon's pid and the pod's restart count
    are unchanged afterwards.

The throwaway agent is `agent-swtest0e7-console`. The namespace runs the camp fixtures
ws-baron / ws-claire / ws-student and nothing here touches them; teardown removes every
`agent-swtest0e7-*` object and runs even when a test fails.
"""

import contextlib
import json
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "control-plane"))

NS = "enterprise-ai"
USER = "swtest0e7"
OTHER = "swtestother"
NAME = "console"

# Spelled out rather than recomputed from app/agents.py: recomputing would make this test
# follow a rename instead of catching one.
OBJ = f"agent-{USER}-{NAME}"

# The Service's own port. The console proxy dials it by number, and `kubectl port-forward`
# is bound to the SAME number locally because the client stack (anyio) takes only the
# address out of name resolution and the port from the URL — a forward on some other local
# port could not be reached by the shipped code without changing the shipped code.
SERVE_PORT = 4096

READY_TIMEOUT_S = 420


def _run(*args, check=True, timeout=700):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                          check=check)


def _kubectl(*args, check=True, timeout=300) -> str:
    return _run("kubectl", "-n", NS, *args, check=check, timeout=timeout).stdout


def _workspace_image() -> str:
    image = _kubectl(
        "get", "pod", "-l", "app.kubernetes.io/component=workspace",
        "-o", 'jsonpath={.items[0].spec.containers[?(@.name=="ttyd")].image}',
    ).strip()
    assert image, "no running workspace pod to read the agent image from"
    return image


def _pod_json() -> dict:
    out = _kubectl(
        "get", "pod",
        "-l", f"agent.enterprise-ai/user={USER},agent.enterprise-ai/name={NAME}",
        "-o", "json",
    )
    items = [p for p in json.loads(out)["items"]
             if (p.get("metadata") or {}).get("deletionTimestamp") is None]
    assert items, f"no pod for {OBJ}"
    return items[0]


def _exec(script: str, check=True) -> subprocess.CompletedProcess:
    return _run("kubectl", "-n", NS, "exec", _pod_json()["metadata"]["name"], "--",
                "bash", "-c", script, check=check)


def resident() -> tuple[str, str, int]:
    """The resident daemon's identity, read off the pod: (pid, start time, restarts).

    THE ground truth for "attach, not spawn". A console that spawned its agent would move
    the pid; a console that restarted it would move the restart count. Both are read with
    kubectl, so neither can be satisfied by anything the proxy returns.
    """
    pid = _exec("pgrep -f 'opencode serve' | head -1").stdout.strip()
    started = _exec(
        "ps -o lstart= -p $(pgrep -f 'opencode serve' | head -1)").stdout.strip()
    restarts = _pod_json()["status"]["containerStatuses"][0]["restartCount"]
    return pid, started, restarts


def _wait_ready(timeout_s=READY_TIMEOUT_S) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            pod = _pod_json()
        except AssertionError:
            time.sleep(3)
            continue
        if pod["status"].get("phase") == "Running" and all(
            c.get("ready") for c in pod["status"].get("containerStatuses", [])
        ):
            return
        time.sleep(3)
    raise AssertionError(f"{OBJ} never became Ready within {timeout_s}s")


def _teardown() -> None:
    """Remove every throwaway object. The camp runs here; leave nothing behind."""
    _kubectl("delete", "deployment", OBJ, "--ignore-not-found", "--wait=true",
             check=False, timeout=300)
    _kubectl("delete", "service", OBJ, "--ignore-not-found", check=False)
    _kubectl("delete", "secret", f"{OBJ}-key", "--ignore-not-found", check=False)
    _kubectl("delete", "pvc", OBJ, "--ignore-not-found", "--wait=true",
             check=False, timeout=300)
    deadline = time.time() + 180
    while time.time() < deadline:
        if not _kubectl("get", "pvc", OBJ, "--ignore-not-found", "-o", "name",
                        check=False).strip():
            return
        time.sleep(2)
    raise AssertionError(f"pvc/{OBJ} is still present 180s after delete")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def _background(*args, ready: callable, what: str, timeout=60):
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            text=True)
    try:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if proc.poll() is not None:
                raise AssertionError(f"{what} exited: {proc.stderr.read()[:500]}")
            if ready():
                break
            time.sleep(0.3)
        else:
            raise AssertionError(f"{what} was not usable within {timeout}s")
        yield proc
    finally:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=10)


def _listening(port: int, host="127.0.0.1") -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


class Console:
    """The live world this test drives: a real pod, a real API server, the new app."""

    def __init__(self, base: str, forwarded: int):
        self.base = base
        self.forwarded = forwarded

    def client(self, user: str) -> httpx.Client:
        """One signed-in browser. Identity is the header oauth2-proxy sets, and it is
        honoured only because this connection comes from 127.0.0.1 — the shipped
        predicate, unmodified."""
        return httpx.Client(base_url=self.base, timeout=30.0,
                            headers={"X-Auth-Request-Preferred-Username": user})

    def direct(self, path: str, **kw) -> httpx.Response:
        """The agent pod itself, bypassing the portal. For measuring the daemon."""
        return httpx.get(f"http://127.0.0.1:{self.forwarded}{path}", timeout=15.0, **kw)


@pytest.fixture(scope="module")
def console():
    """Provision the agent, expose the cluster, and run the NEW portal on loopback."""
    _teardown()
    provisioned = False
    try:
        # AGENT_OPENAI_API_KEY is supplied so the provisioner does NOT mint a virtual key
        # through the deployed control plane. Two reasons, and the first is the binding
        # one: that control plane is fragile right now and this test's rule is to leave it
        # alone. The second is that the deployed image predates Contract 1's `agents/<name>`
        # surface and refuses the mint anyway. Nothing here does inference, so the pod
        # shape — Secret, env, resident daemon — is the one under test either way.
        proc = _run(
            "env",
            f"AGENT_IMAGE={_workspace_image()}",
            "AGENT_OPENAI_API_KEY=placeholder-tests-live-0e7-no-inference-in-this-test",
            "deploy/bin/provision-agent.sh", USER, NAME,
            check=False, timeout=900,
        )
        assert proc.returncode == 0, (
            f"provision-agent.sh failed ({proc.returncode}):\n{proc.stdout}\n{proc.stderr}"
        )
        provisioned = True
        _wait_ready()

        api_port = _free_port()
        with _background(
            "kubectl", "proxy", f"--port={api_port}",
            ready=lambda: _listening(api_port), what="kubectl proxy",
        ), _background(
            "kubectl", "-n", NS, "port-forward", f"svc/{OBJ}",
            f"{SERVE_PORT}:{SERVE_PORT}",
            ready=lambda: _listening(SERVE_PORT), what="kubectl port-forward",
        ):
            yield from _serve(api_port)
    finally:
        if provisioned or _kubectl("get", "deploy", OBJ, "--ignore-not-found", "-o",
                                   "name", check=False).strip():
            _teardown()


def _serve(api_port: int):
    """The shipped app, in this process, pointed at the real cluster."""
    import uvicorn
    from fastapi import FastAPI

    # The DRIVER is stubbed, not the module — the same shape
    # control-plane/tests/test_portal_agents.py uses and for the same reason. `app.db`
    # imports asyncpg at module load, this venv has no database driver, and nothing on the
    # console path opens a connection: attaching reads a Deployment and a Secret from the
    # Kubernetes API and then forwards bytes. Stubbing `app.db` itself would be replacing
    # shipped code; this replaces a package that is never called.
    if "asyncpg" not in sys.modules:
        import types

        pg = types.ModuleType("asyncpg")
        pg.Pool = object

        async def _create_pool(*a, **kw):  # pragma: no cover - never reached
            raise RuntimeError("this suite does not open a database connection")

        pg.create_pool = _create_pool
        sys.modules["asyncpg"] = pg

    from app import agent_console, agent_usage, agents, portal

    sa = Path(tempfile.mkdtemp(prefix="live-sa-"))
    # `kubectl proxy` authenticates on our behalf, so the token's CONTENT is irrelevant —
    # but the shipped code opens the file on every call (projected tokens rotate), and
    # that behaviour must keep being exercised rather than patched out.
    (sa / "token").write_text("through-kubectl-proxy")
    (sa / "namespace").write_text(NS)

    agents.KUBE_API = f"http://127.0.0.1:{api_port}"
    agent_usage.TOKEN_FILE = sa / "token"
    agent_usage.CA_FILE = sa / "ca.crt"
    agent_usage.NAMESPACE_FILE = sa / "namespace"

    # Cluster DNS, and only that. `agent-<user>-<name>` resolves inside the namespace and
    # not on this laptop; the port-forward puts the pod on the Service's own port at
    # 127.0.0.1, so the shipped code dials exactly what it dials in the cluster. A name it
    # derived WRONGLY is not in this map and does not resolve.
    real_getaddrinfo = socket.getaddrinfo

    def fake_getaddrinfo(host, port, *args, **kwargs):
        key = host.decode() if isinstance(host, (bytes, bytearray)) else host
        if key == OBJ:
            return real_getaddrinfo("127.0.0.1", port, *args, **kwargs)
        return real_getaddrinfo(host, port, *args, **kwargs)

    socket.getaddrinfo = fake_getaddrinfo

    api = FastAPI()
    api.include_router(portal.router)
    api.include_router(agent_console.router)

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(api, host="127.0.0.1", port=port,
                                           log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 30
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "the portal never came up on loopback"
    try:
        yield Console(f"http://127.0.0.1:{port}", SERVE_PORT)
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        socket.getaddrinfo = real_getaddrinfo


# ---------------------------------------------------------------- the daemon itself


def test_the_daemon_is_resident_and_refuses_an_unauthenticated_request(console):
    """The two facts every later claim is measured against.

    Resident: `opencode serve` is a direct child of PID 1, so it is what the container
    runs and not something a connection started. Guarded: the same port answers 401
    without a credential. Without the second fact, a 200 through the proxy would prove
    nothing about the proxy at all.
    """
    ps = _exec("ps -eo pid,ppid,args").stdout
    serve = [line for line in ps.splitlines() if "opencode serve" in line]
    assert len(serve) == 1, f"expected exactly one resident opencode serve:\n{ps}"
    assert int(serve[0].split()[1]) == 1, f"opencode serve is not a child of PID 1:\n{ps}"
    assert "ttyd" not in ps, (
        "ttyd is in an agent pod — that is the Code surface's spawn-per-websocket model "
        f"(finding 43) and it is what an Agent must not be:\n{ps}"
    )
    assert console.direct("/app").status_code == 401, (
        "the agent's daemon answered an unauthenticated request. The console credential "
        "would then be decorative and the NetworkPolicy the only lock."
    )


# ---------------------------------------------------------------- attach


def test_the_owner_attaches_to_the_real_console_on_the_portal_origin(console):
    """`/agents/<name>/` returns the resident daemon's own console, under its own prefix.

    Every expected value is taken from the daemon on this same run: the entry document is
    compared against what the pod serves directly, and the asset the rewrite points at is
    fetched back through the proxy and compared byte-for-byte with the pod's copy.
    """
    with console.client(USER) as owner:
        page = owner.get(f"/agents/{NAME}/app")
    assert page.status_code == 200, page.text
    assert "<title>OpenCode</title>" in page.text, page.text[:400]

    # What the daemon really serves, for comparison — read from the pod, not from a
    # fixture in this file.
    password = _run(
        "bash", "-c",
        f"kubectl -n {NS} get secret {OBJ}-key "
        "-o jsonpath='{.data.OPENCODE_SERVER_PASSWORD}' | base64 -d",
    ).stdout.strip()
    assert password, "no OPENCODE_SERVER_PASSWORD in the agent's Secret"
    raw = console.direct("/app", auth=("opencode", password))
    assert raw.status_code == 200

    raw_assets = set(re.findall(r'(?:src|href)="(/[^"/][^"]*)"', raw.text))
    assert raw_assets, f"the daemon's entry document has no root-absolute assets:\n{raw.text[:600]}"
    for asset in raw_assets:
        assert f'"/agents/{NAME}{asset}"' in page.text, (
            f"{asset} was served to the browser unprefixed. At the root of THIS origin it "
            f"is answered by the chat surface, so the console would render blank.\n"
            f"{page.text[:800]}"
        )
    assert not re.search(rf'(src|href)="/(?!agents/{NAME}/)', page.text), page.text[:800]
    assert f'"/agents/{NAME}"' in page.text and "window.fetch=function" in page.text, (
        "the shim is missing, so the compiled bundle would resolve its server as the "
        "origin root and every API call would leave the console's prefix"
    )
    assert password not in page.text, "the daemon's credential reached the browser"

    # The rewritten URL is not a guess: fetch it back through the proxy and compare with
    # the pod's own bytes.
    one = sorted(raw_assets)[0]
    through = None
    with console.client(USER) as owner:
        through = owner.get(f"/agents/{NAME}{one}")
    assert through.status_code == 200, through.text
    assert through.content == console.direct(
        one, auth=("opencode", password)).content, (
        f"{one} came back different through the proxy than from the pod"
    )


def test_the_event_stream_reaches_the_browser_as_the_daemon_emits_it(console):
    """SSE, from the real daemon, relayed rather than buffered.

    `server.connected` is the first thing `opencode serve` writes on `/event` and the
    stream then stays open indefinitely. A proxy that read to completion would hang here
    until the timeout rather than delivering it.
    """
    with console.client(USER) as owner:
        started = time.time()
        with owner.stream("GET", f"/agents/{NAME}/event") as stream:
            assert stream.status_code == 200, stream.read()[:400]
            assert stream.headers["content-type"].startswith("text/event-stream")
            first = next(line for line in stream.iter_lines() if line.strip())
            elapsed = time.time() - started
    assert "server.connected" in first, first
    assert elapsed < 15, (
        f"the daemon's first event took {elapsed:.1f}s to arrive through the proxy on an "
        "endlessly open stream — the response is being buffered"
    )


def test_disconnecting_and_reconnecting_reaches_the_same_resident_agent(console):
    """CONTRACT 2, MEASURED. The claim the whole surface exists to make.

    A session is created THROUGH the console, every connection is then closed — which is
    what closing the browser does — and a second, independent client attaches. What must
    be unchanged is read off the pod: the resident pid, its start time, and the
    container's restart count. What must have survived is read through the console: the
    session created before the disconnect.

    On the Code surface the equivalent measurement fails by construction: ttyd spawns an
    opencode per websocket and it dies with the connection (finding 43). That is the
    comparison this test is making.
    """
    before = resident()
    assert before[0], "no resident opencode serve process before attaching"

    first = console.client(USER)
    assert first.get(f"/agents/{NAME}/app").status_code == 200
    created = first.post(f"/agents/{NAME}/session", json={})
    assert created.status_code in (200, 201), f"{created.status_code}: {created.text[:300]}"
    session_id = created.json()["id"]
    listed = first.get(f"/agents/{NAME}/session")
    assert session_id in [s["id"] for s in listed.json()], listed.text[:300]

    # THE DISCONNECT. Every socket this client held is closed and nothing touches the
    # agent for the length of the window.
    first.close()
    time.sleep(20)

    during = resident()
    assert during[:2] == before[:2], (
        f"the resident daemon changed with nothing attached: {before} -> {during}. The "
        "agent did not survive the console closing, which is the entire difference "
        "between an Agent and the Code surface."
    )

    # THE RECONNECT.
    second = console.client(USER)
    after = resident()
    assert after[0] == before[0], (
        f"reconnecting reached a different process ({before[0]} -> {after[0]}): the "
        "console SPAWNED an agent instead of attaching to the resident one."
    )
    assert after[1] == before[1], f"process start time moved: {before[1]} -> {after[1]}"
    assert after[2] == before[2] == 0, f"the container restarted: {before} -> {after}"

    survived = second.get(f"/agents/{NAME}/session")
    assert survived.status_code == 200, survived.text[:300]
    assert session_id in [s["id"] for s in survived.json()], (
        f"the session created before the disconnect is gone after reconnecting: "
        f"{survived.text[:400]}"
    )
    second.close()


# ---------------------------------------------------------------- reject


def test_a_second_real_identity_cannot_attach_to_this_agents_console(console):
    """The attack, with a real second identity and the real pod as the witness.

    `swtestother` knows the agent is called `console` — the path never carries an owner,
    so that is all anybody needs to guess. The refusal must be a 404, and the proof that
    it was refused is that the resident daemon is untouched afterwards: same pid, same
    start time, same restart count, read with kubectl.
    """
    before = resident()
    with console.client(OTHER) as intruder:
        for path in (f"/agents/{NAME}/app", f"/agents/{NAME}/session",
                     f"/agents/{NAME}/config"):
            resp = intruder.get(path)
            assert resp.status_code == 404, (
                f"{path} as {OTHER} returned {resp.status_code}, not 404 — another "
                f"person's live agent console is reachable by name:\n{resp.text[:300]}"
            )
    after = resident()
    assert after == before, (
        f"{USER}'s daemon changed while {OTHER} was probing it: {before} -> {after}"
    )
    # And the owner still gets in, or the refusal above would be satisfied by a route
    # that refuses everybody.
    with console.client(USER) as owner:
        assert owner.get(f"/agents/{NAME}/app").status_code == 200


def test_an_identity_header_from_off_loopback_is_not_honoured(console):
    """The console is not exempt from portal.py's loopback rule.

    The app is bound to 127.0.0.1 here, so this is asserted where it can be: the same
    request from a non-loopback peer is what `require_user` refuses, and
    control-plane/tests/test_agent_console.py drives that peer address directly. What is
    measured HERE is the other half — that a request with no identity at all is refused
    rather than defaulted.
    """
    with httpx.Client(base_url=console.base, timeout=15.0) as anonymous:
        resp = anonymous.get(f"/agents/{NAME}/app")
    assert resp.status_code == 401, resp.text[:300]


def test_the_camp_fixtures_and_the_frozen_surface_were_not_touched(console):
    """A guard, not a feature: this file provisions infrastructure in a live namespace.

    The camp runs on ws-baron / ws-claire / ws-student tomorrow. Nothing in this suite has
    any business near them, and the cheapest way to know is to look.
    """
    pods = _kubectl("get", "pod", "-l", "app.kubernetes.io/component=workspace",
                    "-o", "jsonpath={range .items[*]}{.metadata.name} "
                          "{.status.phase} {.status.containerStatuses[0].restartCount}\n{end}")
    for line in [line for line in pods.splitlines() if line.strip()]:
        name, phase, _restarts = line.split()
        assert phase == "Running", f"workspace pod {name} is {phase}"

    control_plane = _kubectl(
        "get", "pod", "-l", "app=control-plane",
        "-o", "jsonpath={.items[0].status.phase}").strip()
    assert control_plane == "Running", (
        f"the control-plane pod is {control_plane}. This suite must never restart, roll "
        "or patch it — the camp's login depends on it."
    )
