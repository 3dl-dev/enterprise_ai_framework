"""LIVE: a user's Raven creates a hermes agent and talks to it through the control-plane relay.

Item enterpriseaiframework-692 (agents-raven.md, Contract G). Nothing here mocks the gateway,
the Kubernetes API, the Raven, the hermes agent or the NetworkPolicies. The claims, each read
from an independent source:

  1. EGRESS. A Raven pod reaches the control plane on :8000 and nothing else in the pod
     network (68-raven-common.yaml, the live `raven-isolation` policy). The refusals are
     proven to be that fence's: a THROWAWAY additive egress policy for this Raven alone
     (widened to the pod CIDR; then to one pod) turns exactly the probes it admits into
     connections, so the probes could connect and the Raven's own egress is what stopped them.
  2. CREATE. Driven by a real chat turn over the console proxy, the Raven calls its
     eaf-agents tool, which creates a hermes agent through the agent-manager token API. Read
     with kubectl: the Deployment exists, stamped `created-by: <raven>`, and its key Secret
     carries the API_SERVER_KEY the relay will present.
  3. TURN. A second chat turn: the Raven sends the child a message through the relay and
     reports the child's reply (a word only the child was asked for). The control plane's
     audit has an `agent.relay.turn` row, outcome ok, and no conversation text.
  4. CROSS-OWNER. The same tool, from the Raven pod with the Raven's own token, naming
     ANOTHER user's running hermes agent, is refused 404 and audited `agent-manager.denied`.
  5. DIRECT PATH BLOCKED. From the Raven pod, the child's pod IP on :8642 (and :9119) is
     refused, while the control-plane pod reaches the same :8642 (it is listening). SECOND
     MUTATION, both directions: widening the RAVEN's egress to the pod CIDR does NOT open it
     (the destination fence, 66/63, is what refuses); a throwaway ingress policy admitting
     `type: raven` to THIS child alone DOES open it (the probe could connect all along).

Where the code under test is: RELAY_CP_POD names the control-plane pod to drive (default: the
deployed `app=control-plane` pod). While this branch is not deployed, point it at a throwaway
pod running the branch image, whose AGENT_MANAGER_URL names a Service in front of it:
    RELAY_CP_POD=cp-692 pytest tests-live/test_agent_relay_live.py
Throwaway objects: agents `rv692`, `hc692` (USER) and `xf692` (OTHER), NetworkPolicies
`relay-692-*`; all removed before and after, even on failure. Spends a few cents of inference.
"""
import json
import os
import subprocess
import time

import pytest

NS = "enterprise-ai"
USER = os.environ.get("RELAY_USER", "baron")
OTHER = os.environ.get("RELAY_OTHER_USER", "claire")
RAVEN, CHILD, FOREIGN = "rv692", "hc692", "xf692"
RAVEN_MODEL = os.environ.get("RELAY_RAVEN_MODEL", "anthropic/claude-haiku-4-5")
CHILD_MODEL = os.environ.get("RELAY_CHILD_MODEL", "zai-org/GLM-5.3-Flash")
POLICIES = ("relay-692-egress-podcidr", "relay-692-egress-postgres", "relay-692-ingress-raven")
REFUSED, OPEN = "NETFAIL", "CONNECTED"


def _run(*args, check=True, timeout=600, stdin=None):
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout, input=stdin)
    if check and r.returncode != 0:
        raise AssertionError(f"{' '.join(args[:6])} failed: {r.stderr[-1500:]}{r.stdout[-500:]}")
    return r


def _kubectl(*args, check=True, timeout=300, stdin=None):
    return _run("kubectl", "-n", NS, *args, check=check, timeout=timeout, stdin=stdin).stdout


def _cp_pod() -> str:
    return os.environ.get("RELAY_CP_POD") or _kubectl(
        "get", "pod", "-l", "app=control-plane", "-o", "jsonpath={.items[0].metadata.name}").strip()


def _in_cp(script: str, *argv: str, timeout=900) -> str:
    return _run("kubectl", "-n", NS, "exec", _cp_pod(), "-c", "control-plane", "--",
                "python3", "-c", script, *argv, timeout=timeout).stdout


def _pod_of(name: str, user: str = USER) -> str:
    return _kubectl("get", "pod", "-l",
                    f"agent.enterprise-ai/user={user},agent.enterprise-ai/name={name}",
                    "--field-selector=status.phase=Running",
                    "-o", "jsonpath={.items[0].metadata.name}").strip()


def _ip(pod: str) -> str:
    return _kubectl("get", "pod", pod, "-o", "jsonpath={.status.podIP}").strip()


def _in_raven(*cmd: str, timeout=600) -> str:
    return _kubectl("exec", _pod_of(RAVEN), "--", *cmd, timeout=timeout)


_CALL = """
import json, sys, httpx
user, method, path = sys.argv[1:4]
body = json.loads(sys.argv[4]) if len(sys.argv) > 4 else None
r = httpx.request(method, "http://127.0.0.1:8000" + path,
                  headers={"x-auth-request-preferred-username": user}, json=body, timeout=180)
try:
    payload = r.json()
except ValueError:
    payload = r.text
print(json.dumps({"status": r.status_code, "body": payload}))
"""

# One chat turn in the Raven's console, through the control plane's console proxy, as the
# owner sitting at it: every approval Raven asks for (its default for an MCP tool) is granted
# once, and recorded.
_CHAT = """
import asyncio, json, sys, websockets
user, name, session, prompt = sys.argv[1:5]
H = {"x-auth-request-preferred-username": user}
async def main():
    url = f"ws://127.0.0.1:8000/agents/{name}/rpc"
    try:
        conn = websockets.connect(url, extra_headers=H, max_size=None, open_timeout=30)
    except TypeError:
        conn = websockets.connect(url, additional_headers=H, max_size=None, open_timeout=30)
    async with conn as ws:
        async def call(i, method, params):
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params}))
            while True:
                m = json.loads(await asyncio.wait_for(ws.recv(), 60))
                if m.get("id") == i:
                    return m
        await call(1, "turn.subscribe", {"session_key": session})
        sent = await call(2, "turn.send", {"session_key": session, "content": prompt})
        assert sent["result"]["accepted"] is True, sent
        text, done, results, approvals, n = "", None, [], [], 10
        while done is None:
            m = json.loads(await asyncio.wait_for(ws.recv(), 840))
            if m.get("method") == "approval.request":
                p = m["params"]
                approvals.append(p.get("command"))
                n += 1
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": n, "method": "approval.respond",
                    "params": {"approval_id": p["approval_id"],
                               "session_id": p["conversation_id"], "choice": "allow"}}))
                continue
            ev = (m.get("params") or {}).get("event") or {}
            if ev.get("type") == "token.delta":
                text += ev["payload"]["text"]
            elif ev.get("type") == "tool.complete":
                results.append((ev.get("payload") or {}).get("result_preview"))
            elif ev.get("type") == "message.complete":
                done = ev
        print(json.dumps({"text": text, "results": results, "approvals": approvals}))
asyncio.run(main())
"""

_AUDIT = """
import asyncio, json, os, sys, asyncpg
async def main():
    conn = await asyncpg.connect(os.environ["CONTROL_PLANE_DATABASE_URL"])
    rows = await conn.fetch("SELECT actor, action, target, detail::text AS detail FROM audit_event "
                            "WHERE target = ANY($1::text[]) AND action = ANY($2::text[]) ORDER BY seq",
                            sys.argv[1].split(","), sys.argv[2].split(","))
    print(json.dumps([{**dict(r), "detail": json.loads(r["detail"])} for r in rows]))
    await conn.close()
asyncio.run(main())
"""

_PROBE = """
import json, socket, sys
out = {}
for target in sys.argv[1:]:
    host, port = target.rsplit(":", 1)
    s = socket.socket(); s.settimeout(4)
    try:
        s.connect((host, int(port))); out[target] = "CONNECTED"
    except OSError as exc:
        out[target] = "NETFAIL " + type(exc).__name__
    finally:
        s.close()
print(json.dumps(out))
"""


def portal(user, method, path, body=None):
    args = [user, method, path] + ([json.dumps(body)] if body is not None else [])
    out = json.loads(_in_cp(_CALL, *args))
    return out["status"], out["body"]


def chat(session: str, prompt: str) -> dict:
    return json.loads(_in_cp(_CHAT, USER, RAVEN, session, prompt, timeout=900))


def audit(targets: list[str], actions: list[str]) -> list[dict]:
    return json.loads(_in_cp(_AUDIT, ",".join(targets), ",".join(actions)))


def probe(src_pod: str, *targets: str, container: str | None = None) -> dict:
    args = ["exec", src_pod] + (["-c", container] if container else []) + \
        ["--", "python3", "-c", _PROBE, *targets]
    return {k: v.split()[0] for k, v in json.loads(_kubectl(*args, timeout=120)).items()}


def _policy(name: str, spec: dict):
    _kubectl("apply", "-f", "-", stdin=json.dumps({
        "apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
        "metadata": {"name": name, "namespace": NS}, "spec": spec}))
    time.sleep(5)  # kube-router programs the rule asynchronously


def _drop_policy(name: str):
    _kubectl("delete", "netpol", name, "--ignore-not-found")
    time.sleep(5)


def _raven_selector() -> dict:
    return {"matchLabels": {"agent.enterprise-ai/user": USER, "agent.enterprise-ai/name": RAVEN}}


def _shrink_and_wait(user: str, name: str):
    """The shared node is CPU-packed while other work runs: only the REQUEST is lowered so a
    throwaway agent schedules. Ports, env, policies and the image under test are untouched."""
    obj = f"agent-{user}-{name}"
    _kubectl("patch", "deploy", obj, "--type=json", "-p", json.dumps([{
        "op": "replace", "path": "/spec/template/spec/containers/0/resources/requests/cpu",
        "value": "20m"}]))
    _kubectl("rollout", "status", f"deploy/{obj}", "--timeout=600s", timeout=630)


def _teardown():
    for p in POLICIES:
        _kubectl("delete", "netpol", p, "--ignore-not-found", check=False)
    for user, name in ((USER, CHILD), (USER, RAVEN), (OTHER, FOREIGN)):
        try:
            portal(user, "DELETE", f"/portal/api/agents/{name}")
        except AssertionError:
            pass


@pytest.fixture(scope="module")
def world():
    _teardown()
    status, body = portal(USER, "POST", "/portal/api/agents",
                          {"name": RAVEN, "type": "raven", "model": RAVEN_MODEL})
    assert status == 201, body
    status, body = portal(OTHER, "POST", "/portal/api/agents",
                          {"name": FOREIGN, "type": "hermes", "model": CHILD_MODEL})
    assert status == 201, body
    try:
        _shrink_and_wait(USER, RAVEN)
        _shrink_and_wait(OTHER, FOREIGN)
        yield {}
    finally:
        _teardown()


def test_1_a_raven_reaches_the_control_plane_and_nothing_else_in_the_pod_network(world):
    raven = _pod_of(RAVEN)
    cp = _ip(_cp_pod())
    pg = _ip("postgres-0")
    valkey = _ip(_kubectl("get", "pod", "-l", "app=valkey",
                          "-o", "jsonpath={.items[0].metadata.name}").strip())
    targets = (f"{cp}:8000", f"{pg}:5432", f"{valkey}:6379")

    real = probe(raven, *targets)
    assert real == {targets[0]: OPEN, targets[1]: REFUSED, targets[2]: REFUSED}, real

    # Mutation 1: widen THIS Raven's egress to the whole pod CIDR. Postgres and valkey have
    # no ingress policy of their own, so if they open, the refusal above was the Raven's
    # egress fence and nothing else.
    _policy(POLICIES[0], {"podSelector": _raven_selector(), "policyTypes": ["Egress"],
                          "egress": [{"to": [{"ipBlock": {"cidr": "10.42.0.0/16"}}]}]})
    try:
        widened = probe(raven, *targets)
    finally:
        _drop_policy(POLICIES[0])
    assert widened == {t: OPEN for t in targets}, widened

    # Mutation 2: a nearby, narrower widening (postgres:5432 only) opens exactly that probe.
    _policy(POLICIES[1], {"podSelector": _raven_selector(), "policyTypes": ["Egress"],
                          "egress": [{"to": [{"podSelector": {"matchLabels": {"app": "postgres"}}}],
                                      "ports": [{"protocol": "TCP", "port": 5432}]}]})
    try:
        narrow = probe(raven, *targets)
    finally:
        _drop_policy(POLICIES[1])
    assert narrow == {targets[0]: OPEN, targets[1]: OPEN, targets[2]: REFUSED}, narrow

    assert probe(raven, *targets) == real, "the fence did not return after the mutation"


def test_2_the_raven_creates_a_hermes_child_through_its_tool(world):
    out = chat("relay-692-create",
               f"Use your eaf-agents create_agent tool to create a hermes agent named {CHILD} "
               f"with model {CHILD_MODEL}. Report the tool's result verbatim.")
    assert any("create_agent" in (a or "") and CHILD in a for a in out["approvals"]), out
    assert any(r and json.loads(r).get("created") == CHILD for r in out["results"]), out

    obj = f"agent-{USER}-{CHILD}"
    labels = json.loads(_kubectl("get", "deploy", obj, "-o", "jsonpath={.metadata.labels}"))
    assert labels["agent.enterprise-ai/type"] == "hermes"
    assert labels["agent.enterprise-ai/created-by"] == RAVEN
    keys = json.loads(_kubectl("get", "secret", f"{obj}-key", "-o", "jsonpath={.data}"))
    assert "API_SERVER_KEY" in keys
    _shrink_and_wait(USER, CHILD)


def test_3_the_raven_sends_the_child_a_turn_through_the_relay_and_shows_its_reply(world):
    word = "marmalade"
    before = len(audit([f"{USER}/{CHILD}"], ["agent.relay.turn"]))
    out = None
    for _ in range(3):  # the child's API server may take a minute after the pod is Ready
        out = chat("relay-692-turn",
                   f"Use your eaf-agents send_to_agent tool to send the agent {CHILD} this "
                   f"exact message: 'Reply with exactly one word: {word}'. Then tell me "
                   f"exactly what {CHILD} replied.")
        if any(r and word in r.lower() for r in out["results"]):
            break
        time.sleep(30)
    assert any(r and word in r.lower() for r in out["results"]), out
    assert word in out["text"].lower(), out
    assert any("send_to_agent" in (a or "") for a in out["approvals"]), out

    rows = audit([f"{USER}/{CHILD}"], ["agent.relay.turn"])[before:]
    ok = [r for r in rows if r["detail"]["outcome"] == "ok"]
    assert ok and ok[-1]["actor"] == f"agent-manager:{USER}/{RAVEN}", rows
    assert ok[-1]["detail"]["upstream_status"] == 200 and ok[-1]["detail"]["bytes_out"] > 0
    assert word not in json.dumps(rows), "the audit must not hold the conversation"


def test_4_a_relay_call_naming_another_users_agent_is_refused(world):
    assert _pod_of(FOREIGN, OTHER), "the other user's agent must be running for this to mean anything"
    before = len(audit([f"{USER}/{FOREIGN}"], ["agent-manager.denied"]))
    out = _in_raven("sh", "-c", '/app/.venv/bin/python /opt/eaf/eaf_agents_mcp.py '
                    f'--base-url "$EAF_AGENT_MANAGER_URL" --call send_to_agent {FOREIGN} hello')
    assert out.strip().startswith("refused (404)"), out
    # Positive control, same pod, same tool, same token: its own child answers.
    mine = _in_raven("sh", "-c", '/app/.venv/bin/python /opt/eaf/eaf_agents_mcp.py '
                     f'--base-url "$EAF_AGENT_MANAGER_URL" --call send_to_agent {CHILD} '
                     '"Reply with exactly one word: quince"')
    assert "quince" in mine.lower(), mine
    denied = audit([f"{USER}/{FOREIGN}"], ["agent-manager.denied"])[before:]
    assert [(d["actor"], d["detail"]["status"], d["detail"]["attempted"]) for d in denied] == [
        (f"agent-manager:{USER}/{RAVEN}", 404, "agent.relay")], denied


def test_5_direct_raven_to_hermes_traffic_is_still_blocked_by_the_hermes_fence(world):
    raven = _pod_of(RAVEN)
    child_ip = _ip(_pod_of(CHILD))
    targets = (f"{child_ip}:8642", f"{child_ip}:9119")
    # The control plane reaches the same :8642: something is listening there.
    assert probe(_cp_pod(), targets[0], container="control-plane") == {targets[0]: OPEN}

    real = probe(raven, *targets)
    assert real == {t: REFUSED for t in targets}, real

    # Mutation 1: open the RAVEN's egress to the pod CIDR. Still refused: the fence that
    # stops this is the destination's ingress (66/63), not the Raven's own egress.
    _policy(POLICIES[0], {"podSelector": _raven_selector(), "policyTypes": ["Egress"],
                          "egress": [{"to": [{"ipBlock": {"cidr": "10.42.0.0/16"}}]}]})
    try:
        assert probe(raven, *targets) == {t: REFUSED for t in targets}
        # Mutation 2: a throwaway ingress policy on THIS child admitting type=raven on :8642.
        # Now the same probe connects, so it could have all along.
        _policy(POLICIES[2], {
            "podSelector": {"matchLabels": {"agent.enterprise-ai/user": USER,
                                            "agent.enterprise-ai/name": CHILD}},
            "policyTypes": ["Ingress"],
            "ingress": [{"from": [{"podSelector": {"matchLabels": {
                "agent.enterprise-ai/type": "raven"}}}],
                "ports": [{"protocol": "TCP", "port": 8642}]}]})
        opened = probe(raven, *targets)
    finally:
        _drop_policy(POLICIES[2])
        _drop_policy(POLICIES[0])
    assert opened == {targets[0]: OPEN, targets[1]: REFUSED}, opened
    assert probe(raven, *targets) == real, "the fence did not return after the mutation"
