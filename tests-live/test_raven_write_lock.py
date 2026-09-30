"""LIVE: the console proxy's allow-list refuses every Raven WebUI write outside it.

Items enterpriseaiframework-250 and -7cd (agents-raven.md Contract E, hosted-mode lock).
Nothing here mocks the proxy, the gateway or the Kubernetes API: a Raven is created through
the portal's real `POST /portal/api/agents`, and real JSON-RPC 2.0 frames go over the console
proxy's WebSocket (`/agents/<name>/rpc`) to the real hosted Raven image. The independent
evidence is Raven's own state INSIDE the pod, read with kubectl before and after
(`/data/.raven/config.json`, plus a digest of every file under /data/.raven that is not a
log/session/cache); the refusal frames are the proxy's answer, the files are the truth.

Claims, each proved both ways (fence closed -> refused and files byte-identical; the rules DATA
poisoned -> the same write goes through and config.json changes):

  * the registrations the tests classify are the pod's real ones (the dump script re-run in
    the live pod equals control-plane/tests/fixtures/raven_v0.2.3_rpc_methods.json);
  * the audit's two bypasses -- `config.set {key: "model"}` and `settings.everosSet` -- and
    every other refused write, in their equivalent forms (case, separator, alias, whitespace,
    zero-width, JSON escapes, duplicate members, notification, batch, binary opcode,
    fragmented message, params nesting), are refused BY THIS FENCE (error data
    refused_by=eaf-console-proxy) and change nothing;
  * normal WebUI reads, and a whitelisted setting write, still reach Raven and work.

RAVEN_CP_POD names the control-plane pod to drive. Point it at a throwaway pod running the
branch image (label app=control-plane so the agent's ingress policy admits it, never-ready
probe so the shared Service never routes to it); the rules file is edited in that pod (and
restored), which is why it must be a throwaway. The agent is owned by a synthetic user.
"""
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

NS = "enterprise-ai"
USER = os.environ.get("RAVEN_LIVE_USER", "swtest-7cd")
NAME = "e7cd"
OBJ = f"agent-{USER}-{NAME}"
RULES = "/srv/app/raven_write_lock.json"
CONFIG = "/data/.raven/config.json"
MODEL = os.environ.get("RAVEN_LIVE_MODEL", "anthropic/claude-haiku-4-5")
REPO = Path(__file__).resolve().parents[1]
DUMP = REPO / "deploy" / "raven" / "dump_rpc_methods.py"
FIXTURE = REPO / "control-plane" / "tests" / "fixtures" / "raven_v0.2.3_rpc_methods.json"
CANARY_KEY = "sk-e7cd-canary-provider-key"
CANARY_TOKEN = "e7cd-canary-telegram-token"
CANARY_MODEL = "swtest-7cd-canary-model"


def _run(*args, check=True, timeout=600, stdin=None):
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout, input=stdin)
    if check and r.returncode != 0:
        raise AssertionError(f"{' '.join(args[:7])} failed: {r.stderr[-1500:]}{r.stdout[-500:]}")
    return r


def _kubectl(*args, check=True, timeout=300, stdin=None):
    return _run("kubectl", "-n", NS, *args, check=check, timeout=timeout, stdin=stdin).stdout


def _cp_pod() -> str:
    pod = os.environ.get("RAVEN_CP_POD")
    assert pod, "RAVEN_CP_POD must name a THROWAWAY control-plane pod running this branch"
    return pod


def _in_cp(script: str, *argv: str, timeout=400) -> str:
    return _kubectl("exec", _cp_pod(), "-c", "control-plane", "--", "python3", "-c", script,
                    *argv, timeout=timeout)


def _config() -> str:
    return _kubectl("exec", f"deploy/{OBJ}", "--", "cat", CONFIG)


# Every settings-like file Raven keeps, hashed: config.json, everos.toml, the env mirror,
# the sub-agent/plugin state. Logs, sessions, memory and caches churn on their own.
_STATE = ("cd /data/.raven && find . -type f \\( -name '*.json' -o -name '*.toml' -o -name "
          "'env' -o -name '*.env' -o -name '*.yaml' \\) -not -path './sessions/*' -not -path "
          "'./logs/*' -not -path './everos/*' -not -path '*/cache/*' -not -path './workspace/*' "
          "-not -name '*.lock' -not -name 'serve*.json' | sort | xargs -r sha256sum")


def _state() -> str:
    return _kubectl("exec", f"deploy/{OBJ}", "--", "sh", "-c", _STATE)


_CREATE = """
import json, sys, httpx
r = httpx.request(sys.argv[1], "http://127.0.0.1:8000" + sys.argv[2],
                  headers={"x-auth-request-preferred-username": sys.argv[3]},
                  json=json.loads(sys.argv[4]) if len(sys.argv) > 4 else None, timeout=180)
print(json.dumps({"status": r.status_code, "body": r.text[:300]}))
"""

# Sends each frame over the console proxy's socket and reports the reply that carries its
# id. kind: text | bin | frag (one message split into several websocket continuation frames).
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
            d = f["data"]
            if f["kind"] == "bin":
                await ws.send(d.encode())
            elif f["kind"] == "frag":
                await ws.send([d[i:i + 7] for i in range(0, len(d), 7)])
            else:
                await ws.send(d)
            loop = asyncio.get_event_loop()
            end, got = loop.time() + f.get("timeout", 10), None
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


def _text(method, id, kind="text", **params):
    return {"kind": kind, "wait": id, "data": json.dumps(
        {"jsonrpc": "2.0", "id": id, "method": method, "params": params})}


def _raw(data, wait, kind="text"):
    return {"kind": kind, "wait": wait, "data": data}


def send(*frames):
    return json.loads(_in_cp(_SEND, USER, NAME, json.dumps(list(frames))))


def _by_fence(reply) -> bool:
    items = reply if isinstance(reply, list) else [reply]
    return bool(items) and all(
        isinstance(i, dict) and (i.get("error") or {}).get("code") == -32001
        and (i["error"].get("data") or {}).get("refused_by") == "eaf-console-proxy"
        for i in items)


def _set_model(id, kind="text"):
    return _text("config.set", id, kind, key="model", value=CANARY_MODEL, provider="custom",
                 scope="default")


@pytest.fixture(scope="module")
def raven():
    assert USER.startswith("swtest-"), "test agents are owned by a synthetic user"

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
        end = time.time() + 180   # the page's socket comes up after the pod turns Ready
        while time.time() < end:
            (r,) = send(_text("system.ping", 1))
            if r and "result" in r:
                break
            time.sleep(5)
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


def test_the_classified_registrations_are_the_live_pods_real_ones(raven):
    live = json.loads(_kubectl("exec", "-i", f"deploy/{OBJ}", "--", "python", "-",
                               stdin=DUMP.read_text()))
    assert live == json.loads(FIXTURE.read_text())["methods"]
    for m in ("config.set", "settings.everosSet", "settings.everos_set", "model.save_key"):
        assert m in live


def test_reads_and_a_whitelisted_setting_still_work_through_the_proxy(raven):
    before = _config()
    opts, status, cfg, sget, sessions, theme = send(
        _text("model.options", 1), _text("channels.status", 2), _text("config.get", 3),
        _text("settings.get", 4), _text("session.list", 5),
        _text("config.set", 6, key="tui.theme", value="swtest7cd"))
    assert opts["result"]["provider"] == "custom", opts
    assert any(c["name"] == "telegram" for c in status["result"]["channels"])
    assert "result" in cfg and "result" in sget and "result" in sessions, (cfg, sget, sessions)
    assert theme.get("result", {}).get("applied") is True, theme
    after = _config()
    # The detector is live: an ALLOWED write does change the file the refusals are judged by.
    assert after != before and json.loads(after)["tui"]["theme"] == "swtest7cd"


REFUSED_WRITES = [
    ("model.save_key", {"slug": "openrouter", "api_key": CANARY_KEY}),
    ("model.set_fields", {"slug": "custom", "fields": {"apiBase": "http://evil.invalid"}}),
    ("model.add_endpoint", {"slug": "openrouter", "api_key": CANARY_KEY}),
    ("model.disconnect", {"slug": "custom"}), ("model.set_protocol", {"slug": "custom", "protocol": "x"}),
    ("model.add_model", {"slug": "custom", "model": CANARY_MODEL}),
    ("model.remove_model", {"slug": "custom", "model": MODEL}),
    ("channels.configure", {"name": "telegram", "fields": {"token": CANARY_TOKEN}}),
    ("channels.qr", {"name": "whatsapp"}),
    ("settings.everos_set", {"section": "llm", "model": CANARY_MODEL, "provider": "openrouter"}),
    ("settings.everosSet", {"section": "llm", "model": CANARY_MODEL, "provider": "openrouter"}),
    ("subagents.add", {"preset": "claude-code"}), ("subagents.toggle", {"name": "x", "enabled": True}),
    ("subagents.instance.set_model", {"agent": "x", "model": CANARY_MODEL}),
    ("cli.dispatch", {"argv": ["provider", "add", "openrouter", "--api-key", CANARY_KEY], "width": 80}),
    ("slash.exec", {"command": f"/provider add openrouter --api-key {CANARY_KEY}"}),
    ("command.dispatch", {"command": "provider"}), ("system.upgrade", {}),
    ("plug.install", {"name": "x"}),
]


def test_both_bypasses_and_every_refused_write_are_refused_by_the_fence_and_change_nothing(raven):
    before_cfg, before_state = _config(), _state()
    frames = [_set_model(10), _set_model(11, "frag"), _set_model(12, "bin"),
              _text("config.set", 13, key="model", value=CANARY_MODEL, provider="custom",
                    session_id="s-7cd"),
              _text("config.set", 14, key="agents.defaults.provider", value="openrouter"),
              _text("config.unset", 15, key="model"),
              _text("settings.set", 16, key="embedding", value={"model": "m", "provider": "openrouter"}),
              _text("settings.set", 17, key="channels.telegram.enabled", value=True),
              _text("settings.set", 18, key="sessionTitle.provider", value="openrouter"),
              _text("settings.set", 19, key="tools.web.search.apiKey", value=CANARY_KEY)]
    frames += [_text(m, 100 + i, **p) for i, (m, p) in enumerate(REFUSED_WRITES)]
    frames += [
        _text("settings.EverosSet", 201, section="llm", model=CANARY_MODEL, provider="openrouter"),
        _text("settings.everos-set", 202), _text("Config.set", 203, key="language", value="en"),
        _text("config.set ", 204, key="language", value="en"),
        _text("model.save​_key", 205, slug="openrouter", api_key=CANARY_KEY),
        _text("MODEL.SAVE_KEY", 206, slug="openrouter", api_key=CANARY_KEY),
        _text("model.some_future_write", 207, slug="openrouter", api_key=CANARY_KEY),
        _raw('{"jsonrpc":"2.0","id":208,"method":"config.set","params":{"key":"\\u006dodel",'
             f'"value":"{CANARY_MODEL}","provider":"custom","scope":"default"}}}}', 208),
        _raw('{"jsonrpc":"2.0","id":209,"method":"config.set","params":{"key":"language",'
             f'"key":"model","value":"{CANARY_MODEL}","provider":"custom","scope":"default"}}}}', 209),
        _raw('{"jsonrpc":"2.0","id":210,"method":"config.get","method":"settings.everosSet",'
             f'"params":{{"section":"llm","model":"{CANARY_MODEL}","provider":"openrouter"}}}}', 210),
        _raw(json.dumps({"jsonrpc": "2.0", "id": 211, "method": "config.set",
                         "params": {"params": {"key": "model", "value": CANARY_MODEL}}}), 211),
        _raw(json.dumps([{"jsonrpc": "2.0", "id": 212, "method": "model.options", "params": {}},
                         json.loads(_set_model(213)["data"])]), 212),
    ]
    replies = send(*frames)
    for f, reply in zip(frames, replies):
        assert reply is not None and _by_fence(reply), (f["data"][:100], reply)
    # A notification gets no reply by design; judged by the files alone.
    notif = json.dumps({"jsonrpc": "2.0", "method": "config.set", "params": {
        "key": "model", "value": CANARY_MODEL, "provider": "custom", "scope": "default"}})
    send(_raw(notif, "none", "text") | {"timeout": 3})
    time.sleep(2)
    after = _config()
    assert after == before_cfg, "config.json in the raven pod must be byte-identical"
    assert _state() == before_state, "no settings file under /data/.raven may change"
    for canary in (CANARY_KEY, CANARY_TOKEN, CANARY_MODEL):
        assert canary not in after


def test_poisoning_the_rules_to_allow_config_set_lets_the_model_bypass_through(raven, rules):
    before = _config()
    (r,) = send(_set_model(31))
    assert _by_fence(r) and _config() == before
    rules(lambda raw: raw["allow"].append("config.set"))
    (r,) = send(_set_model(32))
    assert "result" in r and r["result"].get("applied") is True, r
    after = json.loads(_config())
    assert _config() != before and CANARY_MODEL in after["agents"]["defaults"]["model"], after


def test_a_second_poison_widening_the_key_list_lets_the_same_write_through(raven, rules):
    before = _config()
    rules(lambda raw: raw["allow_when_key"]["config.set"].append("model"))
    (r,) = send(_text("config.set", 41, key="model", value=CANARY_MODEL + "-2",
                      provider="custom", scope="default"))
    assert "result" in r, r
    assert _config() != before and CANARY_MODEL + "-2" in _config()


def test_a_third_poison_allowing_the_alias_lets_it_reach_raven(raven, rules):
    rules(lambda raw: raw["allow"].append("settings.everosSet"))
    (alias, twin) = send(
        _text("settings.everosSet", 51, section="llm", model=CANARY_MODEL, provider="custom"),
        _text("settings.everos_set", 52, section="llm", model=CANARY_MODEL, provider="custom"))
    # Raven answers the alias itself (a result, or its own refusal: the hosted pod pins the
    # EverOS roles by env) -- either way it was not this fence. The twin stays fenced.
    assert alias is not None and not _by_fence(alias), alias
    assert _by_fence(twin), twin
