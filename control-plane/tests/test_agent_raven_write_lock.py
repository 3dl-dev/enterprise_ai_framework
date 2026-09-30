"""Hosted-mode lock on Raven's WebUI provider/channel writes (enterpriseaiframework-250).

Two layers, both hermetic; the DONE proof against a real Raven pod is tests-live/
test_raven_write_lock.py.

  * the gate: the verified method list (hand-listed here from the hosted image's dispatcher
    registrations, raven/rpc/methods/*.py v0.2.3, NOT read from the rules file under test),
    plus the bypass shapes;
  * the proxy: a frame the gate refuses is answered to the browser and never reaches the
    (fake) agent, a permitted one does; binary frames get the same treatment.
The rules are DATA: the "both ways" tests poison the rules file the gate reads.
"""
import json

import pytest

from test_agent_raven import OWNER, _serve_ws, app_client, world  # noqa: F401
from app import raven_write_lock

VERIFIED_WRITES = [
    "model.save_key", "model.disconnect", "model.add_model", "model.add_models",
    "model.set_fields", "model.remove_model", "model.set_protocol", "model.oauth_login",
    "model.add_endpoint", "model.remove_endpoint", "channels.configure", "channels.qr",
    "gateway.channels.start", "gateway.channels.qr", "subagents.add", "subagents.update",
    "subagents.toggle", "subagents.remove", "plug.install", "plug.configure",
    "settings.everos_set",
]
READS = ["model.options", "model.fetch_models", "model.endpoints", "channels.status",
         "gateway.channels.live", "turn.send", "turn.subscribe", "session.list", "config.get",
         "config.set", "settings.get", "subagents.list", "cron.list", "system.ping"]


def frame(method, id=1, **params):
    return json.dumps({"jsonrpc": "2.0", "id": id, "method": method, "params": params})


def refused(text):
    out = raven_write_lock.gate_frame(text)
    return out is not None and json.loads(out)


@pytest.fixture(autouse=True)
def _default_rules(monkeypatch):
    monkeypatch.delenv("RAVEN_WRITE_LOCK_FILE", raising=False)


@pytest.mark.parametrize("method", VERIFIED_WRITES)
def test_every_verified_write_method_is_refused_with_its_id(method):
    out = refused(frame(method, id=41))
    assert out["id"] == 41 and out["error"]["code"] == raven_write_lock.REFUSED_CODE
    assert "portal" in out["error"]["message"]


@pytest.mark.parametrize("method", READS)
def test_reads_and_everything_outside_the_lock_pass(method):
    assert raven_write_lock.gate_frame(frame(method)) is None


@pytest.mark.parametrize("method", [
    "model.brand_new_write", "channels.future", "gateway.channels.reload", "model.",
    "MODEL.SAVE_KEY", "Model.Save_Key", " model.save_key", "model.save_key\n", "model .save_key",
    "model.save​_key", "ｍodel.save_key", "Channels.Configure", "MODEL.OPTIONS",
])
def test_unknown_and_disguised_locked_names_fail_closed(method):
    assert refused(frame(method))


def test_a_batch_with_one_write_is_dropped_whole_with_one_error_per_call():
    batch = json.dumps([json.loads(frame("turn.send", id=1)),
                        json.loads(frame("model.save_key", id=2))])
    out = refused(batch)
    assert isinstance(out, list) and [e["id"] for e in out] == [1, 2]
    assert raven_write_lock.gate_frame(json.dumps([json.loads(frame("turn.send"))])) is None


@pytest.mark.parametrize("text", ["not json", "[]", "42", "null", '{"id":1}', '{"method":7}',
                                  '["x"]', '{"method":["model.save_key"]}'])
def test_malformed_frames_fail_closed(text):
    assert refused(text)


@pytest.mark.parametrize("key,value", [
    ("channels.telegram.enabled", True), ("channels.sendProgress", True),
    ("embedding", {"model": "m", "provider": "p"}), ("embedding.provider", "x"),
    ("sessionTitle.provider", "x"), ("context.curatorProvider", "x"),
    ("translate", {"model": "m", "provider": "x"}), ("CHANNELS.slack.enabled", False),
])
def test_settings_set_provider_and_channel_keys_are_refused(key, value):
    assert refused(json.dumps({"jsonrpc": "2.0", "id": 3, "method": "settings.set",
                               "params": {"key": key, "value": value}}))


def test_settings_set_of_an_unrelated_key_passes():
    assert raven_write_lock.gate_frame(json.dumps(
        {"jsonrpc": "2.0", "id": 3, "method": "settings.set",
         "params": {"key": "language", "value": "en"}})) is None


def _rules_file(tmp_path, monkeypatch, mutate):
    raw = json.loads(raven_write_lock._DEFAULT_FILE.read_text())
    mutate(raw)
    p = tmp_path / "rules.json"
    p.write_text(json.dumps(raw))
    monkeypatch.setenv("RAVEN_WRITE_LOCK_FILE", str(p))


def test_the_rules_are_data_unlocking_one_method_needs_both_edits_and_opens_only_it(
        tmp_path, monkeypatch):
    # Removing it from the deny list alone is not enough: the namespace rule still catches
    # it. That is the fail-closed design, asserted first.
    _rules_file(tmp_path, monkeypatch, lambda r: r["deny_methods"].remove("model.save_key"))
    assert refused(frame("model.save_key"))

    def both(raw):
        raw["deny_methods"].remove("model.save_key")
        raw["allow_in_locked_namespaces"].append("model.save_key")
    _rules_file(tmp_path, monkeypatch, both)
    assert raven_write_lock.gate_frame(frame("model.save_key")) is None
    assert refused(frame("model.set_fields")), "only the removed method opened"


def test_dropping_a_namespace_opens_its_unknown_methods(tmp_path, monkeypatch):
    _rules_file(tmp_path, monkeypatch, lambda r: r.update(locked_namespaces=["channels."]))
    assert raven_write_lock.gate_frame(frame("model.brand_new_write")) is None


def test_an_unreadable_rules_file_closes_every_frame(tmp_path, monkeypatch):
    p = tmp_path / "rules.json"
    p.write_text("{not json")
    monkeypatch.setenv("RAVEN_WRITE_LOCK_FILE", str(p))
    assert refused(frame("turn.send"))
    monkeypatch.setenv("RAVEN_WRITE_LOCK_FILE", str(tmp_path / "missing.json"))
    assert refused(frame("turn.send"))


def test_the_shipped_rules_contain_every_verified_write():
    raw = json.loads(raven_write_lock._DEFAULT_FILE.read_text())
    assert set(VERIFIED_WRITES) <= set(raw["deny_methods"])


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
            for i, m in enumerate(["model.save_key", "channels.configure", "MODEL.add_model"]):
                ws.send_text(frame(m, id=100 + i))
                out = json.loads(ws.receive_text())
                assert out["id"] == 100 + i and out["error"]["code"] == -32001
            ws.send_bytes(frame("model.save_key", id=200).encode())  # opcode bypass
            assert json.loads(ws.receive_text())["id"] == 200
            ws.send_bytes(b"\xff\xfe")
            assert json.loads(ws.receive_text())["error"]["code"] == -32001
            ws.send_text(json.dumps([json.loads(frame("turn.send", id=1)),
                                     json.loads(frame("channels.qr", id=2))]))
            assert [e["id"] for e in json.loads(ws.receive_text())] == [1, 2]
            ws.send_text(frame("turn.send", id=9))
            assert json.loads(ws.receive_text())["result"] == "upstream"
        assert [json.loads(f)["method"] for f in seen] == ["turn.send"]
    finally:
        loop.call_soon_threadsafe(loop.stop)
