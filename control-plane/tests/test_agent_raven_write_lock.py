"""Hosted-mode lock on Raven's WebUI (enterpriseaiframework-250, rebuilt as an allow-list by -7cd).

Hermetic; the DONE proof against a real Raven pod is tests-live/test_raven_write_lock.py.

Where the expectations come from, none of them the rules file under test:
  * REGISTERED: the methods the hosted image really registers on its page dispatcher,
    RECORDED by running deploy/raven/dump_rpc_methods.py inside raven-hosted:v0.2.3-eaf2
    (fixtures/raven_v0.2.3_rpc_methods.json; the live test re-dumps a running pod and fails on
    drift). RAVEN_RPC_REGISTRATIONS names another recording, which is how a new registration
    is injected for the "fails until classified" control.
  * SAFE / KEYED / (implicitly) REFUSED: this file's own review of the v0.2.3 handlers
    (upstream a9765e55, raven/rpc/methods/*.py), a second classification kept apart from
    raven_write_lock.json. The two must agree on every registered name, and every registered
    name must be classified by both, so a method a later image adds fails here until someone
    reads its handler.
The rules are DATA: the "both ways" tests poison the rules file the gate reads.
"""
import json
import os
from pathlib import Path

import pytest

from test_agent_raven import OWNER, _serve_ws, app_client, world  # noqa: F401
from app import raven_write_lock

_FIXTURE = Path(__file__).with_name("fixtures") / "raven_v0.2.3_rpc_methods.json"


def _registered() -> list[str]:
    path = Path(os.environ.get("RAVEN_RPC_REGISTRATIONS") or _FIXTURE)
    return json.loads(path.read_text())["methods"]


REGISTERED = _registered()

# Reviewed against the v0.2.3 handlers: none writes a provider, model, channel or embedding
# setting, and none runs the raven CLI. (Some write other state -- sessions, cron, skills,
# knowledge bases, MCP toggles -- which the design keeps editable in the WebUI.)
SAFE = {
    "approval.pending", "approval.respond", "approval.revoke",
    "browser.close", "browser.frame", "browser.input", "browser.mode", "browser.open",
    "browser.read", "browser.state", "browser.tabs", "browser.watch",
    "channels.status", "clarify.respond", "clipboard.paste", "commands.catalog",
    "complete.path", "complete.slash", "config.get", "confirm.respond",
    "cron.delete", "cron.list", "cron.run_now", "cron.runs", "cron.save", "cron.set_enabled",
    "dag.get", "dag.node", "deck.templates.list", "deck.templates.pages", "deck.templates.pick",
    "delegation.pause", "delegation.status", "deliverables.list", "ext.list",
    "fs.dirs", "fs.list", "fs.open", "fs.pick_dir", "fs.read", "fs.reveal", "fs.upload",
    "import.run", "import.scan", "import.status", "import.stop", "input.detect_drop",
    "knowledge.bases.create", "knowledge.bases.delete", "knowledge.bases.list",
    "knowledge.bases.rename", "knowledge.bases.settings", "knowledge.documents.add",
    "knowledge.documents.add_note", "knowledge.documents.add_url", "knowledge.documents.delete",
    "knowledge.documents.index", "knowledge.documents.list", "knowledge.documents.update_note",
    "knowledge.search", "knowledge.status", "memory.list", "memory.stats",
    "model.endpoints", "model.fetch_models", "model.options",
    "playbooks.create", "playbooks.credentials.clear", "playbooks.credentials.get",
    "playbooks.credentials.set", "playbooks.delete", "playbooks.get", "playbooks.list",
    "playbooks.oauth.authorize", "playbooks.oauth.clear", "playbooks.run",
    "playbooks.set_enabled", "playbooks.stints.answer", "playbooks.stints.extend",
    "playbooks.stints.get", "playbooks.stints.list", "playbooks.stints.pause",
    "playbooks.stints.resume", "playbooks.stints.stop", "playbooks.validate",
    "plug.auth", "plug.remove", "plug.retry", "plug.revoke", "plug.toggle",
    "plughub.detail", "plughub.search", "reload.mcp",
    "session.archive", "session.branch", "session.clear", "session.close", "session.compress",
    "session.create", "session.delete", "session.export", "session.interrupt", "session.list",
    "session.most_recent", "session.pin", "session.resume", "session.set_mode",
    "session.status", "session.title", "session.undo", "session.usage",
    "settings.everos", "settings.get", "settings.usage", "setup.status", "shell.exec",
    "skillhub.detail", "skillhub.install", "skillhub.remove", "skillhub.search", "skills.manage",
    "subagent.cancel_instance", "subagent.cancel_session", "subagent.context",
    "subagent.interrupt", "subagent.list",
    "subagents.instance.create", "subagents.instance.forget", "subagents.instance.history",
    "subagents.instance.set_mode", "subagents.instance.steer", "subagents.instances",
    "subagents.list", "subagents.probe", "subagents.test_cancel",
    "system.hello", "system.ping", "system.version", "tasks.list", "terminal.resize",
    "turn.cancel", "turn.send", "turn.subscribe", "turn.unsubscribe",
}
# Dual-use: the key decides. config.set's whitelist is raven/rpc/methods/config.py:131-160 plus
# the special key "model" (-> _set_model, which writes agents.defaults.provider/model);
# settings.set's is console.py:630-989. Only keys that name no provider, model, credential,
# channel or embedding are open.
_CONFIG_KEYS = {"agent.temperature", "tui.theme", "tui.show_token_usage", "language",
                "permissions.mode"}
KEYED = {
    "config.set": _CONFIG_KEYS,
    "config.unset": _CONFIG_KEYS,
    "settings.set": {"language", "cron.defaultTimezone", "plugins.disabled",
                     "tools.disabledTools", "skillForge.blocklist", "tools.exec.timeout",
                     "memory.memoryTopK", "agents.defaults.enablePersonalization",
                     "agents.defaults.reasoningEffort", "permissions.mode",
                     "agents.defaults.maxToolIterations", "agents.defaults.contextWindowTokens",
                     "sessions.autoArchiveAfterDays"},
}
# Named here so the audit's two bypasses and the -250 set can never silently leave the
# refused side; everything registered that is neither SAFE nor KEYED is refused as well.
MUST_REFUSE = {
    "model.save_key", "model.disconnect", "model.add_model", "model.add_models",
    "model.set_fields", "model.remove_model", "model.set_protocol", "model.oauth_login",
    "model.add_endpoint", "model.remove_endpoint", "channels.configure", "channels.qr",
    "settings.everos_set", "settings.everosSet", "subagents.add", "subagents.update",
    "subagents.toggle", "subagents.remove", "subagents.instance.set_model", "subagents.test",
    "subagents.build",
    "plug.install", "plug.configure", "cli.dispatch", "slash.exec", "command.dispatch",
    "system.upgrade",
}
# The -32012 stubs of raven/rpc/methods/_stubs.py: they do nothing, and the allow-list keeps
# only verified handlers, so they are refused too.
_STUBS = {"voice.toggle", "voice.record", "browser.manage", "process.stop", "rollback.list",
          "rollback.diff", "rollback.restore", "spawn_tree.save", "spawn_tree.list",
          "spawn_tree.load", "tools.configure", "session.save", "session.steer",
          "skills.reload", "reload.env", "sudo.respond", "secret.respond", "image.attach",
          "prompt.submit", "prompt.background"}
CANARY = {"slug": "openrouter", "api_key": "sk-7cd-canary"}


def call(method, id=1, **params):
    return {"jsonrpc": "2.0", "id": id, "method": method, "params": params}


def frame(method, id=1, **params):
    return json.dumps(call(method, id, **params))


def refused(text):
    out = raven_write_lock.gate_frame(text)
    return out is not None and json.loads(out)


def passes(text):
    return raven_write_lock.gate_frame(text) is None


@pytest.fixture(autouse=True)
def _default_rules(monkeypatch):
    monkeypatch.delenv("RAVEN_WRITE_LOCK_FILE", raising=False)


# ---- registration-derived classification ------------------------------------------------------

def test_every_registered_method_is_classified_by_this_review():
    unclassified = set(REGISTERED) - SAFE - set(KEYED) - MUST_REFUSE
    stubs = {m for m in unclassified if m in _STUBS}
    assert unclassified == stubs, f"classify these new registrations: {sorted(unclassified - stubs)}"
    assert not (SAFE | set(KEYED) | MUST_REFUSE) - set(REGISTERED), "reviewed a name not registered"


def test_the_rules_file_classifies_every_registered_method_and_nothing_else():
    raw = json.loads(raven_write_lock._DEFAULT_FILE.read_text())
    sections = [set(raw["allow"]), set(raw["allow_when_key"]), set(raw["refuse"])]
    assert set().union(*sections) == set(REGISTERED), "rules and registrations differ"
    assert sum(map(len, sections)) == len(REGISTERED), "a method is in two sections"


@pytest.mark.parametrize("method", sorted(set(REGISTERED) - SAFE - set(KEYED)))
def test_every_registered_method_outside_the_review_is_refused(method):
    out = refused(frame(method, id=41, **CANARY))
    assert out["id"] == 41 and out["error"]["code"] == raven_write_lock.REFUSED_CODE
    assert out["error"]["data"]["method"] == method


@pytest.mark.parametrize("method", sorted(SAFE))
def test_every_reviewed_safe_method_passes(method):
    assert passes(frame(method))


@pytest.mark.parametrize("method,key", [(m, k) for m, ks in sorted(KEYED.items()) for k in sorted(ks)])
def test_dual_use_methods_pass_for_their_reviewed_keys(method, key):
    assert passes(json.dumps(call(method, key=key, value="en")))


# ---- the audit's two bypasses, and their equivalent forms --------------------------------------

@pytest.mark.parametrize("params", [
    {"key": "model", "value": "evil/m", "provider": "openrouter"},
    {"key": "model", "value": "evil/m", "provider": "openrouter", "scope": "default"},
    {"key": "model", "value": "evil/m", "provider": "openrouter", "session_id": "s1"},
    {"key": "Model", "value": "m", "provider": "p"}, {"key": "MODEL", "value": "m"},
    {"key": " model", "value": "m"}, {"key": "model​", "value": "m"},
    {"key": "agents.defaults.model", "value": "m"}, {"key": "agents.defaults.provider", "value": "p"},
    {"key": "provider", "value": "p"}, {"key": "embedding", "value": {}},
    {"key": ["language"], "value": "en"}, {"key": {"k": "language"}, "value": "en"},
    {"key": None, "value": "en"}, {"key": 1, "value": "en"}, {"value": "en"}, {},
    {"params": {"key": "language", "value": "en"}},
])
@pytest.mark.parametrize("method", ["config.set", "config.unset"])
def test_config_set_model_and_every_non_whitelisted_key_form_is_refused(method, params):
    assert refused(json.dumps({"jsonrpc": "2.0", "id": 5, "method": method, "params": params}))


@pytest.mark.parametrize("params", [None, [], ["language", "en"], "language", 0])
def test_dual_use_with_params_that_are_not_an_object_is_refused(params):
    body = {"jsonrpc": "2.0", "id": 5, "method": "config.set"}
    if params is not None:
        body["params"] = params
    assert refused(json.dumps(body))


@pytest.mark.parametrize("key,value", [
    ("channels.telegram.enabled", True), ("channels.sendProgress", True),
    ("channels.sendToolHints", True), ("embedding", {"model": "m", "provider": "p"}),
    ("embedding.provider", "x"), ("embedding.model", "x"), ("sessionTitle.provider", "x"),
    ("sessionTitle", {"model": "m", "provider": "p"}), ("translate", {"model": "m", "provider": "x"}),
    ("context.curatorProvider", "x"), ("context", {"curatorModel": "m", "curatorProvider": "p"}),
    ("skillForge.llmGateProvider", "x"), ("skillForge", {"llmGateModel": "m", "llmGateProvider": "p"}),
    ("tools.web.search.apiKey", "k"), ("tools.web.search.provider", "serper"),
    ("tools.web.providers.tavily.apiKey", "k"), ("tools.media.image", {"model": "m", "quality": "low"}),
    ("tools.media.image.apiKey", "k"), ("CHANNELS.slack.enabled", False), ("Language", "en"),
    ("language ", "en"), ("model", "m"),
])
def test_settings_set_provider_channel_credential_and_variant_keys_are_refused(key, value):
    assert refused(json.dumps(call("settings.set", 3, key=key, value=value)))


@pytest.mark.parametrize("method", [
    "settings.everosSet", "settings.everos_set", "settings.everosset", "settings.EverosSet",
    "settings.everos-set", "settings.everos.set", "Settings.everosSet", "settings.everosSet ",
    "settings.everos​Set", "ｓettings.everosSet", "model.saveKey", "model.save-key",
    "MODEL.SAVE_KEY", "Model.Save_Key", " model.save_key", "model.save_key\n", "model/save_key",
    "model.save​_key", "ｍodel.save_key", "Channels.Configure", "channels.Configure",
    "CONFIG.SET", "Config.set", "config_set", "configSet", "config.set\u0000",
    "cli.Dispatch", "slash.Exec", "model.brand_new_write", "brand.new", "", ".",
    "MODEL.OPTIONS", "Turn.Send", "gateway.channels.start", "gateway.channels.live",
])
def test_unknown_alias_case_and_separator_variants_are_refused(method):
    assert refused(json.dumps(call(method, 7, key="language", value="en", **CANARY)))


def test_json_escapes_and_duplicate_members_are_judged_by_their_decoded_meaning():
    # \\u escapes decode to the same string Raven's json.loads sees.
    assert refused('{"jsonrpc":"2.0","id":1,"method":"config.set",'
                   '"params":{"key":"\\u006dodel","value":"m","provider":"p"}}')
    assert refused('{"jsonrpc":"2.0","id":1,"method":"settings.everos\\u0053et","params":{}}')
    # Duplicate members: Python's json (the gate's AND Raven's, raven/rpc/transports/ws.py)
    # keeps the last one, so the verdict follows the last.
    assert refused('{"jsonrpc":"2.0","id":1,"method":"config.set",'
                   '"params":{"key":"language","key":"model","value":"m","provider":"p"}}')
    assert refused('{"jsonrpc":"2.0","id":1,"method":"config.get","method":"model.save_key",'
                   '"params":{}}')
    assert refused('{"jsonrpc":"2.0","id":1,"method":"config.set","params":{"key":"language"},'
                   '"params":{"key":"model","value":"m","provider":"p"}}')
    assert passes('{"jsonrpc":"2.0","id":1,"method":"config.set",'
                  '"params":{"key":"model","key":"language","value":"en"}}')


@pytest.mark.parametrize("body", [
    {"jsonrpc": "2.0", "method": "config.set", "params": {"key": "model", "value": "m", "provider": "p"}},
    {"jsonrpc": "2.0", "id": None, "method": "settings.everosSet", "params": {}},
    {"method": "model.save_key", "params": CANARY},
])
def test_notifications_are_judged_like_requests(body):
    out = refused(json.dumps(body))
    assert out["id"] is None and out["error"]["code"] == raven_write_lock.REFUSED_CODE


def test_a_batch_with_one_refused_call_is_dropped_whole_with_one_error_per_call():
    batch = json.dumps([call("turn.send", 1),
                        call("config.set", 2, key="model", value="m", provider="p")])
    out = refused(batch)
    assert isinstance(out, list) and [e["id"] for e in out] == [1, 2]
    assert refused(json.dumps([call("turn.send", 1), call("settings.everosSet", 2)]))
    assert refused(json.dumps([call("turn.send", 1), [call("model.save_key", 2)]]))
    assert passes(json.dumps([call("turn.send", 1), call("config.set", 2, key="language", value="en")]))


@pytest.mark.parametrize("text", [
    "not json", "[]", "42", "null", '{"id":1}', '{"method":7}', '["x"]',
    '{"method":["model.save_key"]}', '{"method":{"x":"config.get"}}',
    # Two objects in one frame (newline-delimited): not one JSON value, so refused.
    '{"method":"config.get"}\n{"method":"model.save_key"}',
    '﻿{"jsonrpc":"2.0","id":1,"method":"config.get"}',
])
def test_malformed_frames_fail_closed(text):
    assert refused(text)


# ---- the rules are data: poison the file the gate reads ---------------------------------------

def _rules_file(tmp_path, monkeypatch, mutate):
    raw = json.loads(raven_write_lock._DEFAULT_FILE.read_text())
    mutate(raw)
    p = tmp_path / "rules.json"
    p.write_text(json.dumps(raw))
    monkeypatch.setenv("RAVEN_WRITE_LOCK_FILE", str(p))


def test_poison_allowing_config_set_outright_opens_the_model_bypass(tmp_path, monkeypatch):
    bypass = frame("config.set", key="model", value="m", provider="p")
    assert refused(bypass)
    _rules_file(tmp_path, monkeypatch, lambda r: r["allow"].append("config.set"))
    assert passes(bypass)


def test_a_different_poison_widening_the_key_list_opens_only_that_key(tmp_path, monkeypatch):
    _rules_file(tmp_path, monkeypatch,
                lambda r: r["allow_when_key"]["config.set"].append("model"))
    assert passes(frame("config.set", key="model", value="m", provider="p"))
    assert refused(frame("config.set", key="agents.defaults.provider", value="p"))
    assert refused(frame("settings.everosSet"))


def test_poison_allowing_the_alias_opens_it_and_not_its_twin(tmp_path, monkeypatch):
    _rules_file(tmp_path, monkeypatch, lambda r: r["allow"].append("settings.everosSet"))
    assert passes(frame("settings.everosSet"))
    assert refused(frame("settings.everos_set"))


def test_moving_a_method_to_refuse_documentation_does_not_open_it(tmp_path, monkeypatch):
    # The gate never reads `refuse`: deleting a name from it cannot open anything.
    _rules_file(tmp_path, monkeypatch, lambda r: r["refuse"].pop("model.save_key"))
    assert refused(frame("model.save_key", **CANARY))


@pytest.mark.parametrize("mutate", [
    lambda r: r.update(allow="config.set model.save_key settings.everosSet"),  # substring trap
    lambda r: r.update(allow={"config.set": True}),
    lambda r: r["allow_when_key"].update({"config.set": "model language"}),
    lambda r: r.pop("allow"),
    lambda r: r.pop("allow_when_key"),
])
def test_rules_of_the_wrong_shape_close_every_frame(tmp_path, monkeypatch, mutate):
    _rules_file(tmp_path, monkeypatch, mutate)
    assert refused(frame("turn.send"))
    assert refused(frame("config.set", key="model", value="m", provider="p"))


def test_an_unreadable_rules_file_closes_every_frame(tmp_path, monkeypatch):
    p = tmp_path / "rules.json"
    p.write_text("{not json")
    monkeypatch.setenv("RAVEN_WRITE_LOCK_FILE", str(p))
    assert refused(frame("turn.send"))
    monkeypatch.setenv("RAVEN_WRITE_LOCK_FILE", str(tmp_path / "missing.json"))
    assert refused(frame("turn.send"))


# ---- through the proxy -------------------------------------------------------------------------

def test_through_the_proxy_a_write_is_answered_and_never_forwarded_and_a_read_is(world):
    seen = []

    async def handler(conn):
        async for message in conn:
            seen.append(message)
            await conn.send(json.dumps({"jsonrpc": "2.0", "id": 0, "result": "upstream"}))

    loop = _serve_ws(world, handler)
    try:
        world.add_agent(OWNER, "rv")
        world.hosts["agent-alice-rv"] = "127.0.0.2"
        with app_client(OWNER).websocket_connect("/agents/rv/rpc") as ws:
            for i, text in enumerate([frame("model.save_key", **CANARY),
                                      frame("config.set", key="model", value="m", provider="p"),
                                      frame("settings.everosSet"), frame("MODEL.add_model")]):
                body = json.loads(text)
                body["id"] = 100 + i
                ws.send_text(json.dumps(body))
                out = json.loads(ws.receive_text())
                assert out["id"] == 100 + i and out["error"]["code"] == -32001
            ws.send_bytes(frame("config.set", id=200, key="model", value="m", provider="p").encode())
            assert json.loads(ws.receive_text())["id"] == 200
            ws.send_bytes(b"\xff\xfe")
            assert json.loads(ws.receive_text())["error"]["code"] == -32001
            ws.send_text(json.dumps([call("turn.send", 1), call("channels.qr", 2)]))
            assert [e["id"] for e in json.loads(ws.receive_text())] == [1, 2]
            ws.send_text(frame("turn.send", id=9))
            assert json.loads(ws.receive_text())["result"] == "upstream"
            ws.send_text(frame("config.set", id=10, key="language", value="en"))
            assert json.loads(ws.receive_text())["result"] == "upstream"
        assert [json.loads(f)["method"] for f in seen] == ["turn.send", "config.set"]
        assert json.loads(seen[1])["params"]["key"] == "language"
    finally:
        loop.call_soon_threadsafe(loop.stop)
