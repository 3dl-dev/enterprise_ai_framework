"""A signed-in person gives a REAL agent a Slack workspace, from the browser's endpoint.

Run against the live k3s cluster:  pytest tests-live/test_portal_connectors.py

WHAT THIS PROVES THAT THE HERMETIC SUITE CANNOT

`control-plane/tests/test_portal_connectors.py` proves the endpoint writes the right
object to a fake apiserver. It cannot prove the thing the item is actually about: that the
object it writes is the one the SHIPPED AGENT READS. That claim spans a Secret name in a
Python module, a `secretRef` in `deploy/k8s/64-agent.template.yaml`, an `envFrom` that
Kubernetes evaluates, a pod restart driven by an annotation, and `deploy/agent/agent-slack`
reading `AGENT_SLACK_BOT_TOKEN` out of its own environment. Every one of those is a place
the name can be wrong, and a fake cluster agrees with whatever the code says.

So the credential is written through the portal endpoint and then read back FROM INSIDE
THE POD, by the shipped tool, with `kubectl exec`.

WHY THE APP RUNS HERE AND NOT IN THE CLUSTER

The deployed control-plane image predates this endpoint. Rolling it to test it would be a
deploy, which is the operator's decision and not a test's (ship checklist
enterpriseaiframework-a39). So the SHIPPED app is run on loopback in this process against
the LIVE API server, holding the live `control-plane` ServiceAccount's own token and the
narrow Role in `deploy/k8s/39-control-plane-rbac.yaml` — the same authority the deployed
pod is meant to hold and no more. This is the pattern tests-live/test_agents_e2e.py
established for -ede; it is not a mock of the cluster, it is the app somewhere else.

WHERE EVERY EXPECTED VALUE COMES FROM

Not from `app/agents.py`:

  * the Secret name `agent-baron-connc79-slack` and the key names are spelled out
    literally here and are the ones the TEMPLATE mounts and the TOOL reads. If the module
    renamed them this test fails rather than following the rename;
  * "the pod carries it" is `kubectl exec … agent-slack config`, which reports what the
    process's own environment holds;
  * "it rolled" is a different pod name than before the call, read with kubectl;
  * the cross-user refusal is a SECOND REAL IDENTITY through the shipped `require_user`
    over loopback, and the proof is that the Secret is byte-identical afterwards.

THE THROWAWAY IS `agent-baron-connc79`. The namespace runs the camp fixtures ws-baron /
ws-claire / ws-student and this file never touches them. Teardown removes every
`agent-baron-connc79*` object and the virtual key, and runs even when a test fails.

THE TOKENS BELOW ARE NOT REAL and reach no Slack: the agent's Socket Mode connection will
fail to authenticate, which is irrelevant to the claim. What is under test is that the
credential a user typed into a browser arrives, intact and complete, in the process that
would use it.
"""

import base64
import contextlib
import json
import os
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
NS = "enterprise-ai"
USER = "baron"
OTHER = "claire"
NAME = "connc79"

# Spelled out, never computed from the code under test. These are the names
# deploy/k8s/64-agent.template.yaml mounts and deploy/agent/agent-slack reads.
OBJ = f"agent-{USER}-{NAME}"
SLACK_SECRET = f"{OBJ}-slack"
ALIAS = f"{USER}::agents/{NAME}"

RBAC_MANIFEST = ROOT / "deploy/k8s/39-control-plane-rbac.yaml"
CP_PORT = 18094
READY_TIMEOUT_S = 480

# Where entrypoint.sh mounts the agent tools. The absolute path is used because a
# `kubectl exec` shell does not inherit the PATH the entrypoint exports.
AGENT_SLACK = "/etc/agent/agent-slack"

# Shaped like the real thing, and deliberately not real. A live Slack token committed to a
# public repository is revoked by Slack's own scanner, which is correct of them and a very
# annoying way to discover that a fixture was too realistic.
BOT_TOKEN = "xoxb-000000000000-c79-live-fixture-not-a-real-token"
APP_TOKEN = "xapp-1-A000000-c79-live-fixture-not-a-real-token"
CHANNEL = "C0C79FIXTURE"


# ---------------------------------------------------------------- shelling out


def _run(*args, check=True, timeout=900, **kw):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                          check=check, **kw)


def _kubectl(*args, check=True, timeout=300) -> str:
    return _run("kubectl", "-n", NS, *args, check=check, timeout=timeout).stdout


def _secret_data(secret: str) -> dict[str, str]:
    """A Secret as the POD would see it: decoded, from the live API server."""
    raw = _kubectl("get", "secret", secret, "-o", "jsonpath={.data}", check=False).strip()
    if not raw:
        return {}
    return {k: base64.b64decode(v).decode() for k, v in json.loads(raw).items()}


def _annotation(key: str) -> str:
    return _kubectl(
        "get", "deployment", OBJ, "-o",
        "jsonpath={.spec.template.metadata.annotations." + key.replace("/", "\\/") + "}",
        check=False,
    ).strip()


def _pod_name() -> str:
    return _kubectl(
        "get", "pod", "-l", f"agent.enterprise-ai/name={NAME}",
        "--field-selector=status.phase!=Succeeded,status.phase!=Failed",
        "-o", "jsonpath={.items[0].metadata.name}", check=False,
    ).strip()


def _wait_ready(*, not_pod: str = "", timeout_s=READY_TIMEOUT_S) -> str:
    """A Ready pod whose name is not `not_pod`.

    The second half is what makes "the pod rolled" a measurement: the strategy is
    Recreate, so the new pod has a new name, and waiting for "a Ready pod" without
    excluding the old one would pass instantly against an agent that never restarted.
    """
    deadline = time.time() + timeout_s
    last = ""
    while time.time() < deadline:
        name = _pod_name()
        if name and name != not_pod:
            raw = _kubectl("get", "pod", name, "-o", "json", check=False)
            if raw:
                pod = json.loads(raw)
                statuses = pod["status"].get("containerStatuses", [])
                if pod["status"].get("phase") == "Running" and statuses and all(
                    c.get("ready") for c in statuses
                ):
                    return name
                last = f"{name}: {pod['status'].get('phase')}"
        time.sleep(3)
    raise AssertionError(f"no new Ready pod for {OBJ} within {timeout_s}s (last: {last})")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _listening(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


@contextlib.contextmanager
def _background(*args, ready, what: str, timeout=90):
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


def _api_server() -> tuple[str, str]:
    url = _run("kubectl", "config", "view", "--raw", "-o",
               "jsonpath={.clusters[0].cluster.server}", timeout=120).stdout.strip()
    host, _, port = url.removeprefix("https://").partition(":")
    return host, port or "443"


def _secret_value(secret: str, key: str) -> str:
    raw = _kubectl("get", "secret", secret, "-o", f"jsonpath={{.data.{key}}}",
                   check=False).strip()
    return base64.b64decode(raw).decode() if raw else ""


# ---------------------------------------------------------------- teardown


def _teardown() -> None:
    """Every object this file creates, including the credential. Leave nothing behind."""
    _kubectl("delete", "deployment", OBJ, "--ignore-not-found", "--wait=true",
             check=False, timeout=300)
    _kubectl("delete", "service", OBJ, "--ignore-not-found", check=False)
    for suffix in ("key", "byo", "slack", "discord", "email"):
        _kubectl("delete", "secret", f"{OBJ}-{suffix}", "--ignore-not-found", check=False)
    _kubectl("delete", "pvc", OBJ, "--ignore-not-found", "--wait=true",
             check=False, timeout=300)
    # The virtual key, at the gateway and in the control plane's table. A deleted
    # Deployment does not stop its key spending money.
    pod = _kubectl("get", "pod", "-l", "app=control-plane", "-o",
                   "jsonpath={.items[0].metadata.name}", check=False).strip()
    if pod:
        _run("kubectl", "-n", NS, "exec", pod, "-c", "control-plane", "--", "python3",
             "-c", _CLEANUP.format(alias=ALIAS), check=False, timeout=180)


_CLEANUP = """
import asyncio, os, httpx, asyncpg
async def main():
    base = os.environ.get("GATEWAY_URL", "http://gateway:4000")
    key = os.environ.get("GATEWAY_MASTER_KEY", "")
    try:
        httpx.post(base + "/key/delete", headers={{"Authorization": "Bearer " + key}},
                   json={{"key_aliases": ["{alias}"]}}, timeout=30)
    except Exception:
        pass
    url = os.environ.get("CONTROL_PLANE_DATABASE_URL", "")
    if url:
        conn = await asyncpg.connect(url.replace("postgresql+asyncpg://", "postgresql://"))
        await conn.execute("DELETE FROM virtual_key WHERE key_alias = $1", "{alias}")
        await conn.close()
asyncio.run(main())
"""


# ---------------------------------------------------------------- the live fixture


@pytest.fixture(scope="module")
def base_url():
    """The SHIPPED app on loopback, holding the live cluster's own narrow credential.

    Nothing here patches a function. Every value below is one the app already reads from
    its environment because a pod sets it; the only difference from the deployed pod is
    where the process is.
    """
    import uvicorn

    _teardown()
    sa_dir = Path(tempfile.mkdtemp(prefix="c79-sa-"))
    stack = contextlib.ExitStack()
    applied_rbac = False
    try:
        # The narrow Role the deployed control plane is meant to hold — secrets
        # create/patch/delete included, which is the authority this endpoint uses.
        # Additive, and removed in teardown.
        _run("kubectl", "apply", "-f", str(RBAC_MANIFEST), timeout=180)
        applied_rbac = True
        (sa_dir / "token").write_text(_run(
            "kubectl", "create", "token", "control-plane", "-n", NS,
            "--duration=6h", timeout=120).stdout.strip())
        (sa_dir / "namespace").write_text(NS)
        (sa_dir / "ca.crt").write_text(base64.b64decode(_run(
            "kubectl", "config", "view", "--raw", "-o",
            "jsonpath={.clusters[0].cluster.certificate-authority-data}",
            timeout=120).stdout.strip()).decode())
        sa_dir.chmod(0o700)

        ports = {n: _free_port() for n in ("postgres", "gateway", "chatdb", "identity")}
        for name, target, remote in (("postgres", "pod/postgres-0", 5432),
                                     ("gateway", "svc/gateway", 4000),
                                     ("chatdb", "pod/chatdb-0", 27017),
                                     ("identity", "svc/identity", 8080)):
            stack.enter_context(_background(
                "kubectl", "-n", NS, "port-forward", target, f"{ports[name]}:{remote}",
                ready=lambda p=ports[name]: _listening(p),
                what=f"port-forward {target}"))

        host, port = _api_server()

        def sec(key: str) -> str:
            return _secret_value("enterprise-ai-secrets", key)

        os.environ.update({
            "KUBE_SA_DIR": str(sa_dir),
            "KUBERNETES_SERVICE_HOST": host,
            "KUBERNETES_SERVICE_PORT": port,
            "CONTROL_PLANE_DATABASE_URL": sec("CONTROL_PLANE_DATABASE_URL").replace(
                "@postgres:5432", f"@127.0.0.1:{ports['postgres']}"),
            "GATEWAY_DATABASE_URL": sec("GATEWAY_DATABASE_URL").replace(
                "@postgres:5432", f"@127.0.0.1:{ports['postgres']}"),
            "GATEWAY_URL": f"http://127.0.0.1:{ports['gateway']}",
            "GATEWAY_MASTER_KEY": sec("GATEWAY_MASTER_KEY"),
            "CONTROL_PLANE_ADMIN_TOKEN": sec("CONTROL_PLANE_ADMIN_TOKEN"),
            "CHAT_MONGO_URL": f"mongodb://127.0.0.1:{ports['chatdb']}",
            "CHAT_MONGO_DB": "librechat",
            "IDP_URL": f"http://127.0.0.1:{ports['identity']}",
            "IDP_REALM": "enterprise-ai",
            "IDP_CLIENT_ID": "control-plane",
            "IDP_CLIENT_SECRET": sec("IDP_CLIENT_SECRET"),
            "PORTAL_ADMINS": USER,
            "PUBLISHED_INTERNAL_URL": "http://127.0.0.1:1",
            "WORKSPACE_INTERNAL_TOKEN": sec("WORKSPACE_INTERNAL_TOKEN"),
        })
        import sys
        sys.path.insert(0, str(ROOT / "control-plane"))
        from app.main import app as shipped_app  # imported AFTER the environment is set

        # `KUBERNETES_SERVICE_*` is how client-go decides it is inside a cluster; leaving
        # it set would change what every `kubectl` subprocess below does.
        for leaked in ("KUBERNETES_SERVICE_HOST", "KUBERNETES_SERVICE_PORT"):
            os.environ.pop(leaked, None)

        from app import agent_usage as _usage
        assert _usage.KUBE_API == f"https://{host}:{port}", _usage.KUBE_API

        server = uvicorn.Server(uvicorn.Config(shipped_app, host="127.0.0.1",
                                               port=CP_PORT, log_level="warning"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.time() + 90
        while not server.started and time.time() < deadline:
            time.sleep(0.05)
        assert server.started, "the app under test never came up on loopback"

        import httpx
        probe = httpx.get(f"http://127.0.0.1:{CP_PORT}/openapi.json", timeout=60).json()
        # CHECKED BEFORE ANYTHING DEPENDS ON IT. If this port reached the DEPLOYED control
        # plane — which predates this endpoint — every refusal below would pass for the
        # wrong reason and the file would report a green on code it never ran.
        assert "/portal/api/agents/{name}/connectors" in probe["paths"], (
            f"127.0.0.1:{CP_PORT} is not the app under test; its OpenAPI has no "
            "connector endpoint. Check that nothing else holds that port."
        )
        try:
            yield f"http://127.0.0.1:{CP_PORT}"
        finally:
            server.should_exit = True
            thread.join(timeout=20)
    finally:
        stack.close()
        _teardown()
        if applied_rbac:
            _run("kubectl", "delete", "-f", str(RBAC_MANIFEST), "--ignore-not-found",
                 check=False, timeout=300)
        with contextlib.suppress(OSError):
            for f in sa_dir.iterdir():
                f.unlink()
            sa_dir.rmdir()


def call(base_url, method, path, user=USER, body=None):
    """One request through the shipped stack, as the sidecar makes it.

    Loopback peer and the identity header oauth2-proxy sets — `require_user` derives the
    name, so nothing here hands an endpoint an identity it did not authenticate. That is
    what makes `user=OTHER` below a real second identity and not a parameter.
    """
    import httpx

    r = httpx.request(method, base_url + path, timeout=180,
                      headers={"x-auth-request-preferred-username": user},
                      json=body if body is not None else None)
    try:
        return r.status_code, r.json()
    except Exception:  # noqa: BLE001 - the status is the diagnosis when there is no JSON
        return r.status_code, {"text": r.text[:400]}


# ---------------------------------------------------------------- the run


@pytest.fixture(scope="module")
def wired(base_url):
    """Create the throwaway agent and wire Slack to it — both through the portal.

    Both halves are user-driven POSTs. No kubectl and no provision-agent.sh touches this
    agent, because "a user with only a browser can do this" is the whole claim.
    """
    status, body = call(base_url, "POST", "/portal/api/agents", body={"name": NAME})
    assert status == 201, f"create refused: {status} {body}"

    first = _wait_ready()

    status, body = call(base_url, "POST", f"/portal/api/agents/{NAME}/connectors",
                        body={"kind": "slack", "values": {
                            "AGENT_SLACK_BOT_TOKEN": BOT_TOKEN,
                            "AGENT_SLACK_APP_TOKEN": APP_TOKEN,
                            "AGENT_SLACK_DEFAULT_CHANNEL": CHANNEL,
                        }})
    assert status == 200, f"configure refused: {status} {body}"
    assert BOT_TOKEN not in json.dumps(body), "the endpoint echoed the token back"
    return {"first_pod": first, "response": body}


def test_the_credential_lands_in_the_secret_the_template_mounts(wired):
    """The Secret's NAME and KEYS are the ones the template's `secretRef` names.

    Spelled out here rather than read from `app/agents.py`. A module that wrote
    `agent-baron-connc79-chat` would be self-consistent, would pass every hermetic test,
    and would produce an agent with no Slack at all.
    """
    stored = _secret_data(SLACK_SECRET)
    assert stored, (
        f"no Secret {SLACK_SECRET} on the live cluster; the template mounts that exact "
        f"name with envFrom"
    )
    assert stored["AGENT_SLACK_BOT_TOKEN"] == BOT_TOKEN
    assert stored["AGENT_SLACK_APP_TOKEN"] == APP_TOKEN
    assert stored["AGENT_SLACK_DEFAULT_CHANNEL"] == CHANNEL
    assert set(stored) == {
        "AGENT_SLACK_BOT_TOKEN", "AGENT_SLACK_APP_TOKEN",
        "AGENT_SLACK_DEFAULT_CHANNEL", "AGENT_SLACK_CONFIG_SUM",
    }, (
        f"the Secret carries {sorted(stored)}; every key in it becomes an environment "
        "variable in a pod that holds a spendable model key"
    )
    assert _annotation("checksum/slack") == stored["AGENT_SLACK_CONFIG_SUM"], (
        "the pod-template annotation and the checksum stored beside the credential "
        "disagree, so the next render would roll the agent for a credential that did "
        "not change"
    )
    assert BOT_TOKEN not in _kubectl("get", "deployment", OBJ, "-o", "yaml"), (
        "the credential is in the Deployment, which is readable by anything that can "
        "read Deployments; only its hash belongs there"
    )


def test_the_running_pod_reports_slack_configured_from_its_own_environment(wired):
    """The claim the hermetic suite cannot make: the SHIPPED TOOL sees the credential.

    `agent-slack config` prints what its own process environment holds. Reaching it means
    the Secret name matched the template's `secretRef`, `envFrom` injected it, and the
    pod restarted to pick it up — four separate places the name could have been wrong.

    It reports `bot_token_set: true` rather than the token, which is the same set-once
    rule the endpoint follows: there is nothing anywhere in this surface that prints a
    credential back.
    """
    pod = _wait_ready(not_pod=wired["first_pod"])
    assert pod != wired["first_pod"], "the pod never rolled, so it never read the Secret"

    out = _run("kubectl", "-n", NS, "exec", pod, "--", AGENT_SLACK, "config",
               timeout=180).stdout
    config = json.loads(out)
    assert config["bot_token_set"] is True, f"the pod has no bot token: {config}"
    assert config["app_token_set"] is True, (
        f"the pod has no app-level token, so it can post and can never listen: {config}"
    )
    assert config["default_channel"] == CHANNEL
    assert BOT_TOKEN not in out and APP_TOKEN not in out, (
        "`agent-slack config` printed a credential"
    )


def test_a_second_identity_cannot_wire_another_persons_agent(base_url, wired):
    """THE attack, with a real second identity against a real running agent.

    Succeeding would put an agent holding baron's spendable model key into claire's own
    Slack workspace, taking her instructions, on his bill. The proof of refusal is not the
    status code alone: the Secret is read back from the cluster and must be unchanged.
    """
    before = _secret_data(SLACK_SECRET)
    status, body = call(base_url, "POST", f"/portal/api/agents/{NAME}/connectors",
                        user=OTHER,
                        body={"kind": "slack", "values": {
                            "AGENT_SLACK_BOT_TOKEN": "xoxb-mallory-took-it",
                            "AGENT_SLACK_APP_TOKEN": "xapp-mallory-took-it",
                        }})
    assert status == 404, (
        f"{OTHER} configured {USER}'s agent ({status} {body}) — 404 is also what a "
        "non-existent agent returns, on purpose: a 403 would confirm one exists"
    )
    assert _secret_data(SLACK_SECRET) == before, (
        f"{USER}'s credential was modified by a request from {OTHER}"
    )
    assert _annotation("checksum/slack") == before["AGENT_SLACK_CONFIG_SUM"]


def test_deleting_the_agent_takes_its_chat_credential_with_it(base_url, wired):
    """A left-behind Secret is a live bot token with nothing using it.

    Runs last on purpose: it destroys the subject. The same argument that makes delete
    revoke the virtual key applies to the tenant's own Slack credential.
    """
    status, body = call(base_url, "DELETE", f"/portal/api/agents/{NAME}")
    assert status == 200, f"{status} {body}"
    assert _secret_data(SLACK_SECRET) == {}, (
        f"{SLACK_SECRET} survived the delete: a live bot token with no owner"
    )
    assert _kubectl("get", "deployment", OBJ, "--ignore-not-found").strip() == ""
