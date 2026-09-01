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


def _rollup_transport(monkeypatch, rows):
    def handler(request):
        assert request.url.path == "/api/v1/usage/rollup"
        return httpx.Response(200, json={"data": rows})

    real = httpx.AsyncClient
    monkeypatch.setattr(metering_freerouter.httpx, "AsyncClient",
                        lambda *a, **k: real(*a, transport=httpx.MockTransport(handler), **k))


def test_freerouter_spend_maps_573_rollup_rows(monkeypatch):
    monkeypatch.setenv("FREEROUTER_URL", "http://freerouter:8080")
    monkeypatch.setenv("FREEROUTER_MASTER_KEY", "fr-sk-cp")
    _rollup_transport(monkeypatch, [
        {"account_id": "acc1", "name": "alice::chat", "spend_micro": 2_500_000,
         "input_tokens": 100, "output_tokens": 40, "request_count": 3},
        {"account_id": "acc2", "name": "bob::ide", "spend_micro": 500_000,
         "input_tokens": 10, "output_tokens": 5, "request_count": 1},
    ])
    rows = run(metering_freerouter.spend_by_user_and_surface())
    by = {(r["username"], r["surface"]): r for r in rows}
    assert by[("alice", "chat")]["spend"] == 2.5  # micro-USD -> USD
    assert by[("alice", "chat")]["prompt_tokens"] == 100
    assert by[("alice", "chat")]["requests"] == 3
    assert by[("bob", "ide")]["completion_tokens"] == 5
    tot = run(metering_freerouter.totals())
    assert tot["spend"] == 3.0 and tot["requests"] == 4 and tot["prompt_tokens"] == 110


def test_freerouter_spend_empty_when_router_unreachable(monkeypatch):
    monkeypatch.setenv("FREEROUTER_MASTER_KEY", "fr-sk-cp")

    def boom(request):
        raise httpx.ConnectError("down", request=request)

    real = httpx.AsyncClient
    monkeypatch.setattr(metering_freerouter.httpx, "AsyncClient",
                        lambda *a, **k: real(*a, transport=httpx.MockTransport(boom), **k))
    assert run(metering_freerouter.spend_by_user_and_surface()) == []


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
