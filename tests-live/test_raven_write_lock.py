"""LIVE: the console proxy refuses a real Raven's provider and channel write RPCs.

Item enterpriseaiframework-250 (agents-raven.md Contract E, hosted-mode lock). Nothing here
mocks the proxy, the gateway or the Kubernetes API: a Raven is created through the portal's
real `POST /portal/api/agents`, and real JSON-RPC 2.0 frames go over the console proxy's
WebSocket (`/agents/<name>/rpc`) to the real hosted Raven image. The independent evidence is
`/data/.raven/config.json` INSIDE the Raven pod, read with kubectl before and after; the
refusal frames are the proxy's answer, the file is the truth about whether anything changed.

Claims, each proved both ways (fence closed -> refused and file byte-identical; the rules DATA
poisoned -> the same write goes through and the file changes):

  * a provider write (`model.save_key`) and a channel write (`channels.configure`) are refused;
  * reads (`model.options`, `channels.status`) still work through the proxy;
  * realistic bypasses are refused by the fence, file unchanged: case/whitespace/zero-width
    spellings, an UNKNOWN `model.*` method, a batched array, a binary frame, the generic
    `settings.set` route to a channel;
  * poisoning the rules the gate reads (removing the method from the deny list AND from the
    namespace lock) opens exactly that write, and a second, different poison (dropping the
    channel namespace) opens the channel write.

RAVEN_CP_POD names the control-plane pod to drive. This branch is not deployed, so point it at
a throwaway pod running the branch image (label app=control-plane, never-ready probe):
    RAVEN_CP_POD=cp-250 pytest tests-live/test_raven_write_lock.py
The rules file is edited in that pod (and restored), which is why a throwaway pod is required.
"""
import json
import os
import subprocess
import time

import pytest

NS = "enterprise-ai"
USER = "baron"
NAME = "e250"
OBJ = f"agent-{USER}-{NAME}"
RULES = "/srv/app/raven_write_lock.json"
CONFIG = "/data/.raven/config.json"
MODEL = os.environ.get("RAVEN_LIVE_MODEL", "anthropic/claude-haiku-4-5")
CANARY_KEY = "sk-e250-canary-provider-key"
CANARY_TOKEN = "e250-canary-telegram-token"


def _run(*args, check=True, timeout=600, stdin=None):
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout, input=stdin)
    if check and r.returncode != 0:
        raise AssertionError(f"{' '.join(args[:7])} failed: {r.stderr[-1500:]}{r.stdout[-500:]}")
    return r


def _kubectl(*args, check=True, timeout=300, stdin=None):
    return _run("kubectl", "-n", NS, *args, check=check, timeout=timeout, stdin=stdin).stdout


def _cp_pod() -> str:
    return os.environ.get("RAVEN_CP_POD") or _kubectl(
        "get", "pod", "-l", "app=control-plane", "-o", "jsonpath={.items[0].metadata.name}").strip()


def _in_cp(script: str, *argv: str, timeout=400) -> str:
    return _kubectl("exec", _cp_pod(), "-c", "control-plane", "--", "python3", "-c", script,
                    *argv, timeout=timeout)


def _config() -> str:
    return _kubectl("exec", f"deploy/{OBJ}", "--", "cat", CONFIG)


_CREATE = """
import json, sys, httpx
r = httpx.request(sys.argv[1], "http://127.0.0.1:8000" + sys.argv[2],
                  headers={"x-auth-request-preferred-username": sys.argv[3]},
                  json=json.loads(sys.argv[4]) if len(sys.argv) > 4 else None, timeout=180)
print(json.dumps({"status": r.status_code, "body": r.text[:300]}))
"""

# Sends each frame over the console proxy's socket and reports the reply that carries its id.
_SEND = """
import asyncio, json, sys, websockets
user, name, frames = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
H = {"x-auth-request-preferred-username": user}
async def main():
    url = f"ws://127.0.0.1:8000/agents/{name}/rpc"
    try:
        conn = websockets.connect(url, extra_headers=H, max_size=None, open_timeout=30)
    except TypeError:
        conn = websockets.connect(url, additional_headers=H, max_size=None, open_timeout=30)
    out = []
    async with conn as ws:
        for f in frames:
            await ws.send(f["data"].encode() if f["kind"] == "bin" else f["data"])
            loop = asyncio.get_event_loop()
            end, got = loop.time() + 10, None
            while got is None and loop.time() < end:
                try:
                    m = json.loads(await asyncio.wait_for(ws.recv(), max(end - loop.time(), 0.1)))
                except asyncio.TimeoutError:
                    break
                for i in (m if isinstance(m, list) else [m]):
                    if isinstance(i, dict) and i.get("id") == f["wait"] and ("result" in i or "error" in i):
                        got = m
            out.append(got)
    print(json.dumps(out))
asyncio.run(main())
"""


def _text(method, id, **params):
    return {"kind": "text", "wait": id, "data": json.dumps(
        {"jsonrpc": "2.0", "id": id, "method": method, "params": params})}


def send(*frames):
    return json.loads(_in_cp(_SEND, USER, NAME, json.dumps(list(frames))))


def _is_refusal(reply) -> bool:
    items = reply if isinstance(reply, list) else [reply]
    return all(isinstance(i, dict) and (i.get("error") or {}).get("code") == -32001 for i in items)


def _provider_write(id=3):
    return _text("model.save_key", id, slug="openrouter", api_key=CANARY_KEY)


def _channel_write(id=4):
    return _text("channels.configure", id, name="telegram", fields={"token": CANARY_TOKEN})


@pytest.fixture(scope="module")
def raven():
    def teardown():
        _in_cp(_CREATE, "DELETE", f"/portal/api/agents/{NAME}", USER)
    teardown()
    end = time.time() + 120   # a Terminating PVC would strand the new pod as Pending
    while _kubectl("get", "deploy,pvc", "-l", f"agent.enterprise-ai/name={NAME}", "-o", "name",
                   check=False).strip() and time.time() < end:
        time.sleep(3)
    out = json.loads(_in_cp(_CREATE, "POST", "/portal/api/agents", USER,
                            json.dumps({"name": NAME, "type": "raven", "model": MODEL})))
    assert out["status"] == 201, out
    try:
        _kubectl("rollout", "status", f"deployment/{OBJ}", "--timeout=600s", timeout=630)
        yield
    finally:
        teardown()


@pytest.fixture()
def rules():
    """Edit the rules DATA the gate reads, inside the control-plane pod; restore afterwards."""
    pod = _cp_pod()
    original = _kubectl("exec", pod, "-c", "control-plane", "--", "cat", RULES)

    def write(mutate):
        raw = json.loads(original)
        mutate(raw)
        _kubectl("exec", "-i", pod, "-c", "control-plane", "--", "sh", "-c", f"cat > {RULES}",
                 stdin=json.dumps(raw))
    try:
        yield write
    finally:
        _kubectl("exec", "-i", pod, "-c", "control-plane", "--", "sh", "-c", f"cat > {RULES}",
                 stdin=original)


def test_reads_pass_and_the_provider_and_channel_writes_are_refused_and_config_is_unchanged(raven):
    before = _config()
    assert CANARY_KEY not in before and CANARY_TOKEN not in before
    opts, status, prov, chan = send(_text("model.options", 1), _text("channels.status", 2),
                                    _provider_write(3), _channel_write(4))
    assert opts["result"]["provider"] == "custom", "reads still work through the proxy"
    assert any(c["name"] == "telegram" for c in status["result"]["channels"])
    assert _is_refusal(prov) and prov["id"] == 3 and prov["error"]["data"]["method"] == "model.save_key"
    assert _is_refusal(chan) and chan["id"] == 4
    assert _config() == before, "config.json in the raven pod must be byte-identical"


def test_realistic_bypasses_are_refused_by_the_fence_and_config_is_unchanged(raven):
    before = _config()
    bypasses = [
        _text("MODEL.SAVE_KEY", 11, slug="openrouter", api_key=CANARY_KEY),
        _text(" model.save_key ", 12, slug="openrouter", api_key=CANARY_KEY),
        _text("model.save​_key", 13, slug="openrouter", api_key=CANARY_KEY),
        _text("model.some_future_write", 14, slug="openrouter", api_key=CANARY_KEY),
        _text("Channels.Configure", 15, name="telegram", fields={"token": CANARY_TOKEN}),
        _text("settings.set", 16, key="channels.telegram.enabled", value=True),
        _text("settings.set", 17, key="embedding", value={"model": "m", "provider": "openrouter"}),
        {"kind": "text", "wait": 18, "data": json.dumps([
            {"jsonrpc": "2.0", "id": 18, "method": "model.options", "params": {}},
            {"jsonrpc": "2.0", "id": 19, "method": "model.save_key",
             "params": {"slug": "openrouter", "api_key": CANARY_KEY}}])},
        {"kind": "bin", "wait": 20, "data": json.dumps(
            {"jsonrpc": "2.0", "id": 20, "method": "model.save_key",
             "params": {"slug": "openrouter", "api_key": CANARY_KEY}})},
    ]
    replies = send(*bypasses)
    for frame, reply in zip(bypasses, replies):
        assert reply is not None and _is_refusal(reply), (frame["data"][:80], reply)
    after = _config()
    assert after == before and CANARY_KEY not in after and CANARY_TOKEN not in after


def test_poisoning_the_provider_rule_data_lets_the_provider_write_through(raven, rules):
    before = _config()
    # Half the edit (out of the deny list only) does not open it: the namespace rule holds.
    rules(lambda r: r["deny_methods"].remove("model.save_key"))
    (reply,) = send(_provider_write(31))
    assert _is_refusal(reply) and _config() == before

    def open_it(r):
        r["deny_methods"].remove("model.save_key")
        r["allow_in_locked_namespaces"].append("model.save_key")
    rules(open_it)
    prov, other = send(_provider_write(32), _text("model.set_fields", 33, slug="custom", fields={}))
    assert "result" in prov, prov
    assert CANARY_KEY in _config() and _config() != before, "the write really changed config.json"
    assert _is_refusal(other), "only the removed method opened"


def test_a_different_poison_dropping_the_channel_namespace_lets_the_channel_write_through(
        raven, rules):
    before = _config()
    assert CANARY_TOKEN not in before

    def open_it(r):
        r["deny_methods"].remove("channels.configure")
        r["locked_namespaces"].remove("channels.")
    rules(open_it)
    (chan,) = send(_channel_write(41))
    assert "result" in chan, chan
    assert CANARY_TOKEN in _config() and _config() != before
