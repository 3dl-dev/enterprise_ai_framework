"""Unit tests for app/freerouter.py provisioning — the sub-account model (items 757/1da).

No live server: a stateful httpx.MockTransport stands in for freerouter, faithful to the
live endpoints (POST /api/v1/subaccounts, GET /api/v1/usage/rollup, DELETE
/api/v1/subaccounts/{id}). Runs in `make test`. Each (user,surface) is its own sub-account,
so the rollup attributes the bill and revoke is parent-scoped (no raw key held).
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

import app.freerouter as fr


class FakeFreerouter:
    """Minimal stateful freerouter sub-account model, keyed by account_id."""

    def __init__(self):
        self._accts: dict[str, dict] = {}  # account_id -> {name, usage...}
        self._n = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        if path == "/healthz":
            return httpx.Response(200, json={"ok": True})
        if path == "/api/v1/subaccounts" and request.method == "POST":
            self._n += 1
            acct = f"tenant-{body.get('name','').replace('::','')}-{self._n:04d}"
            self._accts[acct] = {"account_id": acct, "name": body.get("name", ""),
                                 "spend_micro": 0, "input_tokens": 0, "output_tokens": 0,
                                 "request_count": 0}
            return httpx.Response(201, json={"data": {
                "account_id": acct, "api_key": f"fr-sk-{acct}", "name": body.get("name", "")}})
        if path == "/api/v1/usage/rollup" and request.method == "GET":
            return httpx.Response(200, json={"data": list(self._accts.values())})
        if path.startswith("/api/v1/subaccounts/") and request.method == "DELETE":
            acct = path.rsplit("/", 1)[-1]
            if acct not in self._accts:
                return httpx.Response(404, json={"error": "not a descendant"})
            del self._accts[acct]  # revoked → drops from rollup in this fake
            return httpx.Response(200, json={"data": {"account_id": acct, "revoked": True}})
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


def test_generate_key_mints_a_named_subaccount(fake):
    created = run(fr.generate_key(username="bob", surface="chat", idp_user_id="idp-bob", max_budget=25.0))
    assert created["key"].startswith("fr-sk-")
    # token = the durable ACCOUNT_ID the control plane stores; data carries account + label
    assert created["token"] == created["data"]["account_id"]
    assert created["data"]["name"] == "bob::chat"


def test_generate_key_ignores_per_account_budget(fake):
    # per-account budget isn't an M1 feature; mint still succeeds with or without one
    a = run(fr.generate_key(username="bob", surface="ide", idp_user_id="x", max_budget=None))
    assert a["data"]["name"] == "bob::ide"


def test_alias_to_account_id_and_prefix_listing(fake):
    run(fr.generate_key(username="bob", surface="chat", idp_user_id="x", max_budget=None))
    run(fr.generate_key(username="bob", surface="ide", idp_user_id="x", max_budget=None))
    run(fr.generate_key(username="ann", surface="chat", idp_user_id="y", max_budget=None))
    by_alias = run(fr.token_hashes_by_alias())
    assert set(by_alias) == {"bob::chat", "bob::ide", "ann::chat"}
    assert all(v.startswith("tenant-") for v in by_alias.values())
    assert set(run(fr.list_aliases(prefix="bob::"))) == {"bob::chat", "bob::ide"}


def test_delete_by_alias_revokes_the_subaccount(fake):
    run(fr.generate_key(username="bob", surface="chat", idp_user_id="x", max_budget=None))
    res = run(fr.delete_by_aliases(["bob::chat"]))
    assert res["deleted_keys"] == ["bob::chat"]
    assert run(fr.list_aliases(prefix="bob::")) == []


def test_delete_missing_ok_is_not_an_error(fake):
    assert run(fr.delete_by_aliases(["nope::x"], missing_ok=True)) == {"deleted_keys": []}


def test_delete_missing_without_ok_raises(fake):
    with pytest.raises(KeyError):
        run(fr.delete_by_aliases(["nope::x"]))


def test_update_budget_is_unsupported_and_fails_loudly(fake):
    # M1 uses the operator tab; there is no per-account cap endpoint. Never silently no-op.
    with pytest.raises(NotImplementedError):
        run(fr.update_budget("tenant-whatever-0001", 50.0))
