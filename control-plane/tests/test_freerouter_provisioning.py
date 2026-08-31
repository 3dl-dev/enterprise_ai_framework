"""Unit tests for app/freerouter.py — the freerouter provisioning module (item 757).

No live server: a stateful httpx.MockTransport stands in for freerouter, faithful to the
shapes confirmed live against the real binary (see the module docstring). Runs in `make
test`. The complementary live check lives with the spike evidence on the item.

The one behaviour these lock in that a live test cannot assert deterministically:
update_budget must FAIL LOUDLY, because freerouter's PATCH does not update `limit` yet.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

import app.freerouter as fr


class FakeFreerouter:
    """Minimal stateful freerouter: sub-keys under one tenant, keyed by hash."""

    def __init__(self):
        self._keys: dict[str, dict] = {}
        self._n = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        if path == "/healthz":
            return httpx.Response(200, json={"ok": True})
        if path == "/api/v1/keys" and request.method == "POST":
            self._n += 1
            h = f"hash{self._n:04d}"
            obj = {
                "hash": h,
                "name": body.get("name", ""),
                "label": f"sk-…{h}",
                "limit": body.get("limit"),
                "limit_remaining": None,
                "usage": 0,
                "disabled": False,
                "created_at": "2026-08-31T00:00:00Z",
                "include_byok_in_limit": False,
            }
            self._keys[h] = obj
            return httpx.Response(201, json={"data": obj, "key": f"fr-sk-{h}"})
        if path == "/api/v1/keys" and request.method == "GET":
            return httpx.Response(200, json={"data": list(self._keys.values())})
        if path.startswith("/api/v1/keys/"):
            h = path.rsplit("/", 1)[-1]
            if h not in self._keys:
                return httpx.Response(404, json={"error": "no such key"})
            if request.method == "DELETE":
                del self._keys[h]
                return httpx.Response(200, json={"data": {"hash": h}})
            if request.method == "PATCH":
                # Faithful to real freerouter: only name/disabled are honored; `limit` is
                # accepted in the body but IGNORED (the gap update_budget guards against).
                if "name" in body:
                    self._keys[h]["name"] = body["name"]
                if body.get("disabled"):
                    self._keys[h]["disabled"] = True
                return httpx.Response(200, json={"data": self._keys[h]})
        return httpx.Response(500, json={"error": f"unhandled {request.method} {path}"})


@pytest.fixture
def fake(monkeypatch):
    server = FakeFreerouter()
    monkeypatch.setenv("FREEROUTER_URL", "http://freerouter:8080")
    monkeypatch.setenv("FREEROUTER_MASTER_KEY", "fr-sk-controlplane")

    real_client = httpx.AsyncClient

    def client_with_mock(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(server.handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(fr.httpx, "AsyncClient", client_with_mock)
    return server


def run(coro):
    return asyncio.run(coro)


def test_health_true(fake):
    assert run(fr.health()) is True


def test_generate_key_mints_named_sub_key_with_budget(fake):
    created = run(
        fr.generate_key(username="bob", surface="chat", idp_user_id="idp-bob", max_budget=25.0)
    )
    assert created["key"].startswith("fr-sk-")
    assert created["data"]["name"] == "bob::chat"
    assert created["data"]["limit"] == 25.0


def test_generate_key_without_budget_omits_limit(fake):
    created = run(
        fr.generate_key(username="bob", surface="ide", idp_user_id="idp-bob", max_budget=None)
    )
    assert created["data"]["limit"] is None


def test_alias_to_hash_and_prefix_listing(fake):
    run(fr.generate_key(username="bob", surface="chat", idp_user_id="x", max_budget=None))
    run(fr.generate_key(username="bob", surface="ide", idp_user_id="x", max_budget=None))
    run(fr.generate_key(username="ann", surface="chat", idp_user_id="y", max_budget=None))
    by_alias = run(fr.token_hashes_by_alias())
    assert set(by_alias) == {"bob::chat", "bob::ide", "ann::chat"}
    assert set(run(fr.list_aliases(prefix="bob::"))) == {"bob::chat", "bob::ide"}


def test_delete_by_alias_removes_the_key(fake):
    run(fr.generate_key(username="bob", surface="chat", idp_user_id="x", max_budget=None))
    res = run(fr.delete_by_aliases(["bob::chat"]))
    assert res["deleted_keys"] == ["bob::chat"]
    assert run(fr.list_aliases(prefix="bob::")) == []


def test_delete_missing_ok_is_not_an_error(fake):
    assert run(fr.delete_by_aliases(["nope::x"], missing_ok=True)) == {"deleted_keys": []}


def test_delete_missing_without_ok_raises(fake):
    with pytest.raises(KeyError):
        run(fr.delete_by_aliases(["nope::x"]))


def test_update_budget_fails_loudly_until_freerouter_supports_patch_limit(fake):
    created = run(
        fr.generate_key(username="bob", surface="chat", idp_user_id="x", max_budget=10.0)
    )
    h = created["data"]["hash"]
    # freerouter accepts the PATCH but ignores `limit` — update_budget MUST NOT silently
    # report success and leave the old budget in force.
    with pytest.raises(NotImplementedError):
        run(fr.update_budget(h, 50.0))
