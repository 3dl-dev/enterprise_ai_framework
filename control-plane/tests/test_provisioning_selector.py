"""Provisioning selector + freerouter operator-tenant bootstrap (item 757)."""

from __future__ import annotations

import asyncio

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


def test_ensure_operator_tenant_returns_existing_key_without_signup(monkeypatch):
    monkeypatch.setenv("FREEROUTER_MASTER_KEY", "fr-sk-already-have-it")

    def explode(request):  # signup must NOT be called
        raise AssertionError("ensure_operator_tenant signed up despite an existing key")

    real = httpx.AsyncClient
    monkeypatch.setattr(freerouter.httpx, "AsyncClient",
                        lambda *a, **k: real(*a, transport=httpx.MockTransport(explode), **k))
    assert run(freerouter.ensure_operator_tenant()) == "fr-sk-already-have-it"


def test_ensure_operator_tenant_signs_up_when_absent(monkeypatch):
    monkeypatch.delenv("FREEROUTER_MASTER_KEY", raising=False)
    monkeypatch.setenv("FREEROUTER_URL", "http://freerouter:8080")

    def handler(request):
        assert request.url.path == "/api/v1/signup"
        return httpx.Response(201, json={"data": {
            "account_id": "tenant-enterprise-ai-control-plane-abc",
            "parent_account_id": "op-root",
            "api_key": "fr-sk-newly-minted",
        }})

    real = httpx.AsyncClient
    monkeypatch.setattr(freerouter.httpx, "AsyncClient",
                        lambda *a, **k: real(*a, transport=httpx.MockTransport(handler), **k))
    assert run(freerouter.ensure_operator_tenant()) == "fr-sk-newly-minted"
