"""Provisioning selector + freerouter operator-tenant bootstrap (item 757)."""

from __future__ import annotations

import asyncio
import os

import httpx
import pytest

from app import freerouter, gateway, provisioning


def run(coro):
    return asyncio.run(coro)


def test_backend_defaults_to_litellm(monkeypatch):
    monkeypatch.delenv("GATEWAY_PROVIDER", raising=False)
    assert provisioning.backend() is gateway


def test_backend_selects_freerouter(monkeypatch):
    monkeypatch.setenv("GATEWAY_PROVIDER", "freerouter")
    assert provisioning.backend() is freerouter


def test_backend_unknown_value_falls_back_to_litellm(monkeypatch):
    monkeypatch.setenv("GATEWAY_PROVIDER", "banana")
    assert provisioning.backend() is gateway


def test_generate_key_dispatches_to_selected_backend(monkeypatch):
    calls: list[str] = []

    async def fr_gen(**kw):
        calls.append("freerouter")
        return {"ok": True}

    monkeypatch.setenv("GATEWAY_PROVIDER", "freerouter")
    monkeypatch.setattr(freerouter, "generate_key", fr_gen)
    run(provisioning.generate_key(username="u", surface="chat", idp_user_id="i", max_budget=None))
    assert calls == ["freerouter"]


def _signup_transport(monkeypatch, api_key="fr-sk-newly-minted"):
    def handler(request):
        assert request.url.path == "/api/v1/signup"
        return httpx.Response(201, json={"data": {
            "account_id": "tenant-enterprise-ai-control-plane-abc",
            "parent_account_id": "op-root",
            "api_key": api_key,
        }})

    real = httpx.AsyncClient
    monkeypatch.setattr(freerouter.httpx, "AsyncClient",
                        lambda *a, **k: real(*a, transport=httpx.MockTransport(handler), **k))


def _no_signup_transport(monkeypatch):
    def explode(request):
        raise AssertionError("bootstrap signed up despite an existing key")

    real = httpx.AsyncClient
    monkeypatch.setattr(freerouter.httpx, "AsyncClient",
                        lambda *a, **k: real(*a, transport=httpx.MockTransport(explode), **k))


def test_bootstrap_returns_injected_secret_without_signup(monkeypatch):
    monkeypatch.setenv("FREEROUTER_MASTER_KEY", "fr-sk-already-have-it")
    monkeypatch.delenv("FREEROUTER_MASTER_KEY_FILE", raising=False)
    _no_signup_transport(monkeypatch)
    assert run(freerouter.bootstrap_master_key()) == "fr-sk-already-have-it"


def test_bootstrap_signs_up_and_persists_to_keyfile(monkeypatch, tmp_path):
    monkeypatch.delenv("FREEROUTER_MASTER_KEY", raising=False)
    monkeypatch.delenv("FREEROUTER_OPERATOR_KEY", raising=False)  # bare gateway: signup fallback
    monkeypatch.setenv("FREEROUTER_URL", "http://freerouter:8080")
    keyfile = tmp_path / "sub" / "operator.key"
    monkeypatch.setenv("FREEROUTER_MASTER_KEY_FILE", str(keyfile))
    _signup_transport(monkeypatch)
    got = run(freerouter.bootstrap_master_key())
    assert got == "fr-sk-newly-minted"
    assert keyfile.read_text().strip() == "fr-sk-newly-minted"
    assert os.environ["FREEROUTER_MASTER_KEY"] == "fr-sk-newly-minted"


def test_bootstrap_reuses_persisted_keyfile_without_signup(monkeypatch, tmp_path):
    monkeypatch.delenv("FREEROUTER_MASTER_KEY", raising=False)
    keyfile = tmp_path / "operator.key"
    keyfile.write_text("fr-sk-from-disk\n")
    monkeypatch.setenv("FREEROUTER_MASTER_KEY_FILE", str(keyfile))
    _no_signup_transport(monkeypatch)  # must NOT sign up — the key is on disk
    assert run(freerouter.bootstrap_master_key()) == "fr-sk-from-disk"
    assert os.environ["FREEROUTER_MASTER_KEY"] == "fr-sk-from-disk"


def test_bootstrap_with_an_operator_key_hangs_the_tenant_under_op_root(monkeypatch, tmp_path):
    """The tenant every EAF key bills through must sit UNDER the operator root, or the operator
    tab is unreachable and agents 402 on an empty standalone balance (seen live 2026-09-29).
    With FREEROUTER_OPERATOR_KEY it is a sub-account of op-root — never a self-serve signup."""
    monkeypatch.delenv("FREEROUTER_MASTER_KEY", raising=False)
    monkeypatch.setenv("FREEROUTER_OPERATOR_KEY", "fr-sk-op-root")
    monkeypatch.setenv("FREEROUTER_URL", "http://freerouter:8080")
    keyfile = tmp_path / "operator.key"
    monkeypatch.setenv("FREEROUTER_MASTER_KEY_FILE", str(keyfile))
    seen = []

    def handler(request):
        seen.append((request.url.path, request.headers.get("authorization")))
        assert request.url.path == "/api/v1/subaccounts", "must not self-serve signup"
        return httpx.Response(201, json={"data": {
            "account_id": "tenant-cp", "api_key": "fr-sk-cp-under-root",
            "name": freerouter.CONTROL_PLANE_TENANT_NAME}})

    real = httpx.AsyncClient
    monkeypatch.setattr(freerouter.httpx, "AsyncClient",
                        lambda *a, **k: real(*a, transport=httpx.MockTransport(handler), **k))
    assert run(freerouter.bootstrap_master_key()) == "fr-sk-cp-under-root"
    assert seen == [("/api/v1/subaccounts", "Bearer fr-sk-op-root")]
    assert keyfile.read_text().strip() == "fr-sk-cp-under-root"


def _credits_transport(monkeypatch, balance_micro, account):
    monkeypatch.setenv("FREEROUTER_MASTER_KEY", "fr-sk-cp")

    def handler(request):
        assert request.url.path == "/api/v1/credits"
        assert request.headers["authorization"] == "Bearer fr-sk-cp"
        return httpx.Response(200, json={"data": {
            "total_credits": balance_micro / 1e6, "balance_micro": balance_micro,
            "billing_account_id": account}})

    real = httpx.AsyncClient
    monkeypatch.setattr(freerouter.httpx, "AsyncClient",
                        lambda *a, **k: real(*a, transport=httpx.MockTransport(handler), **k))


@pytest.mark.parametrize("balance_micro,account,tab,status,headroom", [
    (18_791, "op-root", "100000000", "ok", 100.018791),
    (-97_000_000, "op-root", "100000000", "low", 3.0),
    (-100_000_000, "op-root", "100000000", "exhausted", 0.0),
    (32_296, "tenant-enterprise-ai-control-pl-59455eb3ab82", "100000000", "not_on_tab", 0.032296),
    (5_000_000, "op-root", "", "no_tab", 5.0),
])
def test_the_credit_line_says_where_spend_lands_and_what_is_left(
        monkeypatch, balance_micro, account, tab, status, headroom):
    monkeypatch.setenv("FREEROUTER_OPERATOR_TAB_MICRO", tab)
    monkeypatch.delenv("FREEROUTER_OPERATOR_ROOT_ID", raising=False)
    _credits_transport(monkeypatch, balance_micro, account)
    c = run(freerouter.credit_line())
    assert c["status"] == status
    assert c["billing_account_id"] == account
    assert c["headroom_usd"] == pytest.approx(headroom)
    if status == "not_on_tab":
        assert c["credit_line_usd"] == 0, "a tab the keys cannot reach is not credit they have"
