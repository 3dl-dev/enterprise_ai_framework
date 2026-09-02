"""Unit tests for app/freerouter.py provisioning — the sub-account model (items 757/1da).

No live server: a stateful httpx.MockTransport stands in for freerouter, faithful to the
live endpoints (POST /api/v1/subaccounts, POST/GET /api/v1/keys, GET /api/v1/usage/rollup,
DELETE /api/v1/subaccounts/{id}). Runs in `make test`. Each (user,surface) is its own
sub-account, so the rollup attributes the bill and revoke is parent-scoped (no raw key held).

THE DOUBLE IS NOT THE EVIDENCE. It is here for speed and for the branches that are awkward
to stage live; every claim it makes about freerouter's behaviour is also made against the
real `cmd/freerouter` binary in control-plane/tests/test_freerouter_mirror.py, and that file
is what settles a disagreement. Two of the behaviours reproduced below were measured there
and are the reason this file was updated rather than written from the docs:

  * the rollup carries ONLY accounts that have recorded spend, so a freshly-minted
    sub-account is absent from it (freerouter metering/rollup.go aggregates usage events);
  * a per-key cap is set at mint by the SUB-ACCOUNT itself, is whole USD, ceil()-rounded,
    and renders as null when unlimited (internal/core/keys.go limitToMonthlyUSD).
"""

from __future__ import annotations

import asyncio
import json
import math

import httpx
import pytest

import app.freerouter as fr


class FakeFreerouter:
    """Minimal stateful freerouter sub-account model, keyed by account_id."""

    def __init__(self):
        self._accts: dict[str, dict] = {}  # account_id -> {name, usage...}
        self._keys: dict[str, list[dict]] = {}  # account_id -> minted keys
        self._bearers: dict[str, str] = {}  # bearer -> account_id
        self._n = 0
        # Which accounts have SPENT. The rollup is derived from this and not from _accts,
        # because freerouter's is derived from recorded generations — an account that has
        # never spent is simply not in the rollup. `spend()` is how a test opts one in.
        self._spent: set[str] = set()

    def spend(self, account_id: str) -> None:
        self._spent.add(account_id)

    def _caller(self, request: httpx.Request) -> str | None:
        bearer = request.headers.get("authorization", "").removeprefix("Bearer ")
        return self._bearers.get(bearer)

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
            bearer = f"fr-sk-{acct}"
            self._bearers[bearer] = acct
            self._keys[acct] = []
            return httpx.Response(201, json={"data": {
                "account_id": acct, "api_key": bearer, "name": body.get("name", "")}})
        if path == "/api/v1/keys" and request.method == "POST":
            # Scoped to the CALLER, exactly as freerouter scopes it: the key lands under
            # whichever account the bearer resolves to, which is why generate_key must use
            # the sub-account's bearer and not the control plane's.
            acct = self._caller(request)
            if acct is None:
                return httpx.Response(401, json={"error": "no tenant in context"})
            limit = body.get("limit")
            if limit is not None and limit < 0:
                return httpx.Response(400, json={"error": "limit must not be negative"})
            self._n += 1
            obj = {
                "hash": f"hash-{acct}-{self._n:04d}",
                "name": body.get("name", ""),
                "label": "sk-…deadbeef",
                # null, never 0, when unlimited.
                "limit": (float(math.ceil(limit)) if limit else None),
                "usage": 0,
                "disabled": False,
            }
            self._keys[acct].append(obj)
            return httpx.Response(
                201, json={"data": obj, "key": f"fr-sk-user-{obj['hash']}"}
            )
        if path == "/api/v1/keys" and request.method == "GET":
            acct = self._caller(request)
            if acct is None:
                return httpx.Response(401, json={"error": "no tenant in context"})
            return httpx.Response(200, json={"data": self._keys.get(acct, [])})
        if path == "/api/v1/usage/rollup" and request.method == "GET":
            return httpx.Response(200, json={"data": [
                a for a in self._accts.values() if a["account_id"] in self._spent
            ]})
        if path.startswith("/api/v1/subaccounts/") and request.method == "DELETE":
            acct = path.rsplit("/", 1)[-1]
            if acct not in self._accts:
                return httpx.Response(404, json={"error": "not a descendant"})
            del self._accts[acct]
            self._spent.discard(acct)
            return httpx.Response(200, json={"data": {"account_id": acct, "revoked": True}})
        return httpx.Response(500, json={"error": f"unhandled {request.method} {path}"})


@pytest.fixture
def fake(monkeypatch):
    server = FakeFreerouter()
    monkeypatch.setenv("FREEROUTER_URL", "http://freerouter:8080")
    monkeypatch.setenv("FREEROUTER_MASTER_KEY", "fr-sk-controlplane")
    # A sibling module (test_freerouter_mirror) installs a database-backed alias resolver on
    # app.freerouter at import. These tests are about the freerouter-side read on its own, so
    # the hook is cleared for them rather than left to depend on collection order.
    monkeypatch.setattr(fr, "alias_resolver", None)
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


def test_generate_key_caps_the_user_key_under_the_subaccount(fake):
    """The budget rides on the KEY, minted by the sub-account, because that is freerouter's
    only cap. The key handed back must be that capped key and not the account root bearer —
    returning the root bearer would hand the user an UNCAPPED credential."""
    created = run(fr.generate_key(username="bob", surface="chat", idp_user_id="x", max_budget=25.0))
    account_id = created["token"]
    assert created["key"] != f"fr-sk-{account_id}"  # not the sub-account's root bearer
    assert created["limit_usd"] == 25
    assert [k["limit"] for k in fake._keys[account_id]] == [25.0]
    assert [k["name"] for k in fake._keys[account_id]] == ["bob::chat"]


def test_generate_key_rounds_a_sub_dollar_budget_up(fake):
    """freerouter's cap is whole USD. Rounding UP keeps the mirrored cap looser than the
    original, so a mirrored user never loses spend they already had."""
    created = run(fr.generate_key(username="bob", surface="ide", idp_user_id="x", max_budget=0.5))
    assert created["limit_usd"] == 1


def test_generate_key_without_a_budget_is_unlimited(fake):
    created = run(fr.generate_key(username="bob", surface="ide", idp_user_id="x", max_budget=None))
    assert created["data"]["name"] == "bob::ide"
    assert created["limit_usd"] is None
    assert fake._keys[created["token"]][0]["limit"] is None


def test_alias_to_account_id_and_prefix_listing(fake):
    a = run(fr.generate_key(username="bob", surface="chat", idp_user_id="x", max_budget=None))
    b = run(fr.generate_key(username="bob", surface="ide", idp_user_id="x", max_budget=None))
    c = run(fr.generate_key(username="ann", surface="chat", idp_user_id="y", max_budget=None))
    # The rollup only carries accounts that have SPENT — see the module docstring. Without
    # this, the reads below see nothing, which is the defect app/mirror's resolver closes.
    for created in (a, b, c):
        fake.spend(created["token"])
    by_alias = run(fr.token_hashes_by_alias())
    assert set(by_alias) == {"bob::chat", "bob::ide", "ann::chat"}
    assert all(v.startswith("tenant-") for v in by_alias.values())
    assert set(run(fr.list_aliases(prefix="bob::"))) == {"bob::chat", "bob::ide"}


def test_an_unspent_subaccount_is_invisible_to_the_rollup(fake):
    """Pinned deliberately: this is freerouter's real behaviour and the reason app/mirror
    installs a database-backed alias resolver instead of trusting the rollup as a census."""
    run(fr.generate_key(username="bob", surface="chat", idp_user_id="x", max_budget=None))
    assert run(fr.list_aliases()) == []
    assert run(fr.token_hashes_by_alias()) == {}


def test_delete_by_alias_revokes_the_subaccount(fake):
    created = run(fr.generate_key(username="bob", surface="chat", idp_user_id="x", max_budget=None))
    fake.spend(created["token"])
    res = run(fr.delete_by_aliases(["bob::chat"]))
    assert res["deleted_keys"] == ["bob::chat"]
    assert run(fr.list_aliases(prefix="bob::")) == []


def test_delete_missing_ok_is_not_an_error(fake):
    assert run(fr.delete_by_aliases(["nope::x"], missing_ok=True)) == {"deleted_keys": []}


def test_delete_missing_without_ok_raises(fake):
    with pytest.raises(KeyError):
        run(fr.delete_by_aliases(["nope::x"]))


def test_update_budget_is_unsupported_and_fails_loudly(fake):
    # A cap is fixed at mint: PATCH /api/v1/keys/{hash} carries no limit. Never silently
    # no-op — /admin/budget must not report success for a cap nothing will enforce.
    with pytest.raises(NotImplementedError):
        run(fr.update_budget("tenant-whatever-0001", 50.0))


def test_budget_to_monthly_usd_matches_freerouters_own_conversion(fake):
    # freerouter: `if limit == nil || *limit <= 0 { return 0 }; return ceil(*limit)`, and 0
    # renders as null. Hand-checked against internal/core/keys.go limitToMonthlyUSD and
    # exercised end-to-end against the real binary in test_freerouter_mirror.py.
    assert fr.budget_to_monthly_usd(None) is None
    assert fr.budget_to_monthly_usd(0) is None
    assert fr.budget_to_monthly_usd(-5.0) is None
    assert fr.budget_to_monthly_usd(0.01) == 1
    assert fr.budget_to_monthly_usd(3.25) == 4
    assert fr.budget_to_monthly_usd(25.0) == 25
