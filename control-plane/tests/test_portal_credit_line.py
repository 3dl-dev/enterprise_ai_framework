"""The operator view shows the credit line every key draws on.

Without it the operator tab is invisible, and the first sign it is unreachable (the control
plane billing to a standalone prepaid tenant) or spent is an agent answering 402 — the dead
end found live on 2026-09-29.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from app import agent_usage, freerouter, metering_select, portal, provisioning

STATIC = Path(portal.__file__).resolve().parent / "portal_static"


def _stub_overview_sources(monkeypatch):
    async def empty(*a, **k):
        return []

    async def totals(*a, **k):
        return {"spend": 0.0}

    monkeypatch.setattr(metering_select, "spend_by_user_and_surface", empty)
    monkeypatch.setattr(metering_select, "unpriced_models", empty)
    monkeypatch.setattr(metering_select, "totals", totals)
    monkeypatch.setattr(provisioning, "list_keys", empty)
    monkeypatch.setattr(agent_usage, "usage_by_agent", empty)


def test_the_operator_overview_carries_the_credit_line_on_freerouter(monkeypatch):
    _stub_overview_sources(monkeypatch)
    monkeypatch.setenv("GATEWAY_PROVIDER", "freerouter")
    line = {"status": "not_on_tab", "billing_account_id": "tenant-x"}

    async def credit_line():
        return line

    monkeypatch.setattr(freerouter, "credit_line", credit_line)
    body = asyncio.run(portal.admin_overview(since=None, admin="baron"))
    assert body["credit"] == line


def test_an_unreadable_credit_line_is_shown_not_swallowed(monkeypatch):
    _stub_overview_sources(monkeypatch)
    monkeypatch.setenv("GATEWAY_PROVIDER", "freerouter")

    async def broken():
        raise RuntimeError("gateway down")

    monkeypatch.setattr(freerouter, "credit_line", broken)
    body = asyncio.run(portal.admin_overview(since=None, admin="baron"))
    assert body["credit"]["status"] == "unknown"
    assert "gateway down" in body["credit"]["error"]


def test_there_is_no_credit_line_on_the_litellm_backend(monkeypatch):
    _stub_overview_sources(monkeypatch)
    monkeypatch.delenv("GATEWAY_PROVIDER", raising=False)
    body = asyncio.run(portal.admin_overview(since=None, admin="baron"))
    assert body["credit"] is None


def test_the_operator_page_renders_every_credit_status():
    index = (STATIC / "index.html").read_text()
    js = (STATIC / "app.js").read_text()
    assert 'id="admin-credit"' in index
    assert "renderCredit(d.credit)" in js
    for status in ("ok", "low", "exhausted", "not_on_tab", "no_tab", "unknown"):
        assert f"{status}: (c) =>" in js, f"the page has no wording for credit status {status!r}"
