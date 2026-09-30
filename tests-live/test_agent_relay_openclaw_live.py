"""LIVE: a user's Raven creates an OpenClaw agent and talks to it through the control-plane relay.

Item enterpriseaiframework-b12 (agents-raven.md, Contract G, openclaw row). Nothing here mocks
the gateway, the Kubernetes API, the Raven, the openclaw agent or the control plane. Every
object belongs to the SYNTHETIC owners swtest-b12 / swtest-b12x, never a real user. The claims:

  1. CREATE. Driven by a real chat turn over the console proxy, the Raven calls its eaf-agents
     create_agent tool for type=openclaw through the agent-manager token API. Read with
     kubectl: the Deployment exists, type openclaw, stamped `created-by: <raven>`. The
     approval prompt DID fire for create_agent: it stays at Raven's `ask` tier.
  2. TURN. A second chat turn: the Raven sends the openclaw child a message through the relay
     and reports its reply (a word only the child was asked for). NO approval fired for
     send_to_agent or list_agents (pre-approved in hosted mode), so unattended and voice use
     do not block. The audit has an `agent.relay.turn` row, outcome ok, no conversation text.
  3. CONFIG. The Raven's own config.json on its PVC carries the tiers the seed pins.
  4. CROSS-OWNER. The same tool, from the Raven pod with the Raven's own token, naming ANOTHER
     user's running openclaw agent, is refused 404 and audited `agent-manager.denied`; the
     positive control (its own child) answers from the same pod.

RELAY_CP_POD names the throwaway control-plane pod running the branch image (default: the
deployed app=control-plane pod). Throwaway agents: rv-b12, oc-b12 (swtest-b12), xf-b12
(swtest-b12x); removed before and after, even on failure. Spends a few cents of inference.
"""
import json
import os
import subprocess
import time

import pytest

NS = "enterprise-ai"
USER = os.environ.get("RELAY_USER", "swtest-b12")
OTHER = os.environ.get("RELAY_OTHER_USER", "swtest-b12x")
RAVEN, CHILD, FOREIGN = "rv-b12", "oc-b12", "xf-b12"
RAVEN_MODEL = os.environ.get("RELAY_RAVEN_MODEL", "anthropic/claude-haiku-4-5")
CHILD_MODEL = os.environ.get("RELAY_CHILD_MODEL", "zai-org/GLM-5.3-Flash")


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


def _shrink_and_wait(user: str, name: str):
    """The shared node is CPU-packed while other work runs: only the REQUEST is lowered so a
    throwaway agent schedules. Ports, env, policies and the image under test are untouched."""
    obj = f"agent-{user}-{name}"
    _kubectl("patch", "deploy", obj, "--type=json", "-p", json.dumps([{
        "op": "replace", "path": "/spec/template/spec/containers/0/resources/requests/cpu",
        "value": "20m"}]))
    _kubectl("rollout", "status", f"deploy/{obj}", "--timeout=600s", timeout=630)


def _teardown():
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
                          {"name": FOREIGN, "type": "openclaw", "model": CHILD_MODEL})
    assert status == 201, body
    try:
        _shrink_and_wait(USER, RAVEN)
        _shrink_and_wait(OTHER, FOREIGN)
        yield {}
    finally:
        _teardown()


def _approved(out: dict, tool: str) -> bool:
    return any(tool in (a or "") for a in out["approvals"])


def test_1_the_raven_creates_an_openclaw_child_through_its_tool_and_create_still_asks(world):
    out = chat("relay-b12-create",
               f"Use your eaf-agents create_agent tool to create an openclaw agent named {CHILD} "
               f"with model {CHILD_MODEL}. Report the tool's result verbatim.")
    assert _approved(out, "create_agent"), ("create_agent must still ask the owner", out)
    assert any(r and json.loads(r).get("created") == CHILD for r in out["results"]), out

    obj = f"agent-{USER}-{CHILD}"
    labels = json.loads(_kubectl("get", "deploy", obj, "-o", "jsonpath={.metadata.labels}"))
    assert labels["agent.enterprise-ai/type"] == "openclaw"
    assert labels["agent.enterprise-ai/created-by"] == RAVEN
    _shrink_and_wait(USER, CHILD)


def test_2_the_raven_relays_a_turn_to_the_openclaw_child_without_asking_and_shows_its_reply(world):
    word = "marmalade"
    before = len(audit([f"{USER}/{CHILD}"], ["agent.relay.turn"]))
    out = None
    for _ in range(4):  # the child's gateway may take a minute after the pod is Ready
        out = chat("relay-b12-turn",
                   f"Use your eaf-agents send_to_agent tool to send the agent {CHILD} this "
                   f"exact message: 'Reply with exactly one word: {word}'. Then tell me "
                   f"exactly what {CHILD} replied.")
        if any(r and word in r.lower() for r in out["results"]):
            break
        time.sleep(30)
    assert any(r and word in r.lower() for r in out["results"]), out
    assert word in out["text"].lower(), out
    assert not _approved(out, "send_to_agent"), ("relaying is pre-approved", out)

    listing = chat("relay-b12-list",
                   "Use your eaf-agents list_agents tool and tell me the names of my agents.")
    assert CHILD in listing["text"], listing
    assert not _approved(listing, "list_agents"), ("listing is pre-approved", listing)

    rows = audit([f"{USER}/{CHILD}"], ["agent.relay.turn"])[before:]
    ok = [r for r in rows if r["detail"]["outcome"] == "ok"]
    assert ok and ok[-1]["actor"] == f"agent-manager:{USER}/{RAVEN}", rows
    assert ok[-1]["detail"]["upstream_status"] == 200 and ok[-1]["detail"]["bytes_out"] > 0
    assert word not in json.dumps(rows), "the audit must not hold the conversation"


def test_3_the_ravens_own_config_pins_the_tiers(world):
    cfg = json.loads(_in_raven("sh", "-c", "cat ${RAVEN_HOME:-/data/.raven}/config.json"))
    tiers = cfg["permissions"]["tools"]
    assert tiers["mcp_eaf-agents_list_agents"] == "allow"
    assert tiers["mcp_eaf-agents_send_to_agent"] == "allow"
    assert tiers["mcp_eaf-agents_create_agent"] == "ask"


def test_4_a_relay_call_naming_another_users_openclaw_agent_is_refused(world):
    assert _pod_of(FOREIGN, OTHER), "the other user's agent must be running for this to mean anything"
    before = len(audit([f"{USER}/{FOREIGN}"], ["agent-manager.denied"]))
    tool = ('/app/.venv/bin/python /opt/eaf/eaf_agents_mcp.py '
            '--base-url "$EAF_AGENT_MANAGER_URL" --call send_to_agent')
    out = _in_raven("sh", "-c", f"{tool} {FOREIGN} hello")
    assert out.strip().startswith("refused (404)"), out
    # Positive control, same pod, same tool, same token: its own child answers.
    mine = _in_raven("sh", "-c", f'{tool} {CHILD} "Reply with exactly one word: quince"')
    assert "quince" in mine.lower(), mine
    denied = audit([f"{USER}/{FOREIGN}"], ["agent-manager.denied"])[before:]
    assert [(d["actor"], d["detail"]["status"], d["detail"]["attempted"]) for d in denied] == [
        (f"agent-manager:{USER}/{RAVEN}", 404, "agent.relay")], denied
