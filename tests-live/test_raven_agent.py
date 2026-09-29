"""LIVE: a Raven created through the portal answers a chat turn in its console and is billed.

Item enterpriseaiframework-f16 (agents-raven.md Contract E). Nothing here mocks the gateway or
the Kubernetes API. The claims, each read from an independent source:

  * `POST /portal/api/agents {type: raven}` creates the object set (read with `kubectl`,
    object names spelled out literally, not recomputed from app/agents.py);
  * the console proxy at /agents/<name>/ carries ONE real chat turn: JSON-RPC over the
    WebSocket at /rpc, a `token.delta` stream and a `message.complete`, with the browser
    (this script) never holding the console token;
  * the freerouter's own usage rollup (the ledger, not the portal) has a row for the alias
    `<user>::agents/<name>` with requests > 0 after the turn;
  * a second identity gets 404 on the console, stop and delete, and the first user's
    Deployment is still at replicas=1 afterwards (read with kubectl);
  * stop -> 0 replicas and no pod; start -> Ready again; delete -> every object gone.

Where the code under test is: RAVEN_CP_POD names the control-plane pod to drive (default: the
deployed `app=control-plane` pod). While this branch is not deployed, point it at a throwaway
pod running the branch's image:  RAVEN_CP_POD=cp-f16 pytest tests-live/test_raven_agent.py
"""
import json
import os
import subprocess
import time

import pytest

NS = "enterprise-ai"
USER = "baron"
OTHER = "mallory"
NAME = "f16live"
OBJ = f"agent-{USER}-{NAME}"
ALIAS = f"{USER}::agents/{NAME}"
MODEL = os.environ.get("RAVEN_LIVE_MODEL", "anthropic/claude-haiku-4-5")


def _run(*args, check=True, timeout=600):
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise AssertionError(f"{' '.join(args[:6])} failed: {r.stderr[-1500:]}{r.stdout[-500:]}")
    return r


def _kubectl(*args, check=True, timeout=300):
    return _run("kubectl", "-n", NS, *args, check=check, timeout=timeout).stdout


def _cp_pod() -> str:
    return os.environ.get("RAVEN_CP_POD") or _kubectl(
        "get", "pod", "-l", "app=control-plane", "-o", "jsonpath={.items[0].metadata.name}").strip()


def _in_cp(script: str, *argv: str, timeout=400) -> str:
    return _run("kubectl", "-n", NS, "exec", _cp_pod(), "-c", "control-plane", "--",
                "python3", "-c", script, *argv, timeout=timeout).stdout


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

_CHAT = """
import asyncio, json, sys, websockets
user, name, prompt = sys.argv[1:4]
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
        sk = "f16-live-session"
        await call(1, "turn.subscribe", {"session_key": sk})
        sent = await call(2, "turn.send", {"session_key": sk, "content": prompt})
        assert sent["result"]["accepted"] is True, sent
        text, done = "", None
        while done is None:
            m = json.loads(await asyncio.wait_for(ws.recv(), 150))
            ev = (m.get("params") or {}).get("event") or {}
            if ev.get("type") == "token.delta":
                text += ev["payload"]["text"]
            elif ev.get("type") == "message.complete":
                done = ev
        print(json.dumps({"text": text, "complete": done["type"]}))
asyncio.run(main())
"""

_LEDGER = """
import json, os, sys, httpx
key = os.environ.get("FREEROUTER_MASTER_KEY") or open(os.environ["FREEROUTER_MASTER_KEY_FILE"]).read().strip()
r = httpx.get(os.environ["FREEROUTER_URL"] + "/api/v1/usage/rollup",
              headers={"Authorization": "Bearer " + key}, timeout=60)
data = r.json()
rows = data if isinstance(data, list) else next(v for v in data.values() if isinstance(v, list))
print(json.dumps([x for x in rows if isinstance(x, dict) and x.get("name") == sys.argv[1]]))
"""


def portal(user, method, path, body=None):
    args = [user, method, path] + ([json.dumps(body)] if body is not None else [])
    out = json.loads(_in_cp(_CALL, *args))
    return out["status"], out["body"]


def _objects() -> dict:
    return {kind: _kubectl("get", kind, "-l", f"agent.enterprise-ai/name={NAME}", "-o",
                           "jsonpath={.items[*].metadata.name}", check=False).split()
            for kind in ("deployment", "service", "pvc", "secret", "configmap")}


def _teardown():
    portal(USER, "DELETE", f"/portal/api/agents/{NAME}")


@pytest.fixture(scope="module")
def raven():
    _teardown()
    status, body = portal(USER, "POST", "/portal/api/agents",
                          {"name": NAME, "type": "raven", "model": MODEL})
    assert status == 201, body
    assert body["alias"] == ALIAS and body["type"] == "raven"
    try:
        _kubectl("rollout", "status", f"deployment/{OBJ}", "--timeout=600s", timeout=630)
        yield body
    finally:
        _teardown()


def test_the_object_set_exists_with_the_raven_labels(raven):
    objs = _objects()
    assert objs["deployment"] == [OBJ] and objs["service"] == [OBJ] and objs["pvc"] == [OBJ]
    assert sorted(objs["secret"]) == [f"{OBJ}-key"] and objs["configmap"] == [f"{OBJ}-config"]
    labels = json.loads(_kubectl("get", "deployment", OBJ, "-o", "jsonpath={.spec.template.metadata.labels}"))
    assert labels["agent.enterprise-ai/type"] == "raven"
    assert labels["app.kubernetes.io/component"] == "agent"
    assert _kubectl("get", "svc", OBJ, "-o", "jsonpath={.spec.ports[0].port}") == "18793"


def _ledger_totals() -> tuple[int, int]:
    """(requests, spend_micro) summed over every ledger account carrying the alias. Deleting an
    agent leaves its freerouter account behind, so earlier runs' rows are still there and the
    claim is measured as a DELTA across the turn, not as a bare non-zero."""
    rows = json.loads(_in_cp(_LEDGER, ALIAS))
    return sum(r["request_count"] for r in rows), sum(r["spend_micro"] for r in rows)


def test_the_console_answers_one_chat_turn_and_the_alias_has_a_ledger_row(raven):
    before = _ledger_totals()
    out = json.loads(_in_cp(_CHAT, USER, NAME, "Reply with exactly the word: pong"))
    assert "pong" in out["text"].lower(), out
    assert out["complete"] == "message.complete"
    after = _ledger_totals()
    assert after[0] > before[0] and after[1] > before[1], (before, after)


def test_a_second_identity_cannot_reach_stop_or_delete_it(raven):
    for method, path in (("GET", f"/agents/{NAME}/"), ("GET", f"/agents/{NAME}/health"),
                         ("POST", f"/portal/api/agents/{NAME}/stop"),
                         ("DELETE", f"/portal/api/agents/{NAME}")):
        status, _ = portal(OTHER, method, path)
        assert status == 404, (method, path, status)
    assert portal(OTHER, "GET", "/portal/api/agents")[1]["agents"] == []
    assert _kubectl("get", "deployment", OBJ, "-o", "jsonpath={.spec.replicas}") == "1"


def test_stop_start_and_delete_are_real(raven):
    assert portal(USER, "POST", f"/portal/api/agents/{NAME}/stop")[0] == 200
    assert _kubectl("get", "deployment", OBJ, "-o", "jsonpath={.spec.replicas}") == "0"
    assert _kubectl("get", "pvc", OBJ, "-o", "jsonpath={.metadata.name}") == OBJ, "stop keeps the volume"
    assert portal(USER, "POST", f"/portal/api/agents/{NAME}/start")[0] == 200
    _kubectl("rollout", "status", f"deployment/{OBJ}", "--timeout=600s", timeout=630)
    status, body = portal(USER, "DELETE", f"/portal/api/agents/{NAME}")
    assert status == 200 and body["deleted"] is True and body["key_revoked"] == ALIAS
    # The PVC may report Terminating for a few seconds; the delete endpoint says so honestly.
    end = time.time() + 90
    while any(_objects().values()) and time.time() < end:
        time.sleep(3)
    assert not any(_objects().values()), _objects()
