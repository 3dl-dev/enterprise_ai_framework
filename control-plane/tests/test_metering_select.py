"""Usage-read selector + freerouter backend (item 6cc)."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app import metering, metering_freerouter, metering_select


def run(coro):
    return asyncio.run(coro)


def test_backend_defaults_to_litellm_metering(monkeypatch):
    monkeypatch.delenv("GATEWAY_PROVIDER", raising=False)
    assert metering_select.backend() is metering


def test_backend_selects_freerouter(monkeypatch):
    monkeypatch.setenv("GATEWAY_PROVIDER", "freerouter")
    assert metering_select.backend() is metering_freerouter


def test_freerouter_spend_is_empty_pending_573(monkeypatch):
    # Honest blank bill on the new backend beats reading stale LiteLLM numbers after a flip.
    assert run(metering_freerouter.spend_by_user_and_surface()) == []
    assert run(metering_freerouter.totals()) == {
        "requests": 0, "spend": 0.0, "prompt_tokens": 0, "completion_tokens": 0,
    }


def test_freerouter_unpriced_is_empty_by_invariant():
    # freerouter's price-equality invariant makes an unpriced-but-served model impossible.
    assert run(metering_freerouter.unpriced_models()) == []


def test_freerouter_ledger_ready_reflects_healthz(monkeypatch):
    monkeypatch.setenv("FREEROUTER_URL", "http://freerouter:8080")

    def ok(request):
        assert request.url.path == "/healthz"
        return httpx.Response(200, json={"ok": True})

    real = httpx.AsyncClient
    monkeypatch.setattr(metering_freerouter.httpx, "AsyncClient",
                        lambda *a, **k: real(*a, transport=httpx.MockTransport(ok), **k))
    assert run(metering_freerouter.ledger_ready()) is True


def test_freerouter_ledger_ready_false_when_unreachable(monkeypatch):
    def boom(request):
        raise httpx.ConnectError("down", request=request)

    real = httpx.AsyncClient
    monkeypatch.setattr(metering_freerouter.httpx, "AsyncClient",
                        lambda *a, **k: real(*a, transport=httpx.MockTransport(boom), **k))
    assert run(metering_freerouter.ledger_ready()) is False
