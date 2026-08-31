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
