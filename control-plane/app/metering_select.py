"""Usage-read backend selector — LiteLLM (metering) or freerouter (metering_freerouter).

The read-side twin of app/provisioning: the operator bill / portal / analytics read through
here. Default 'litellm' through the transition, so switching every call site to this module
changes no behaviour until the flip. item 6cc.

item 730 — continuous history across the flip. Before the flip, LiteLLM is the only backend
with any rows and freerouter's rollup is honestly empty (nothing has ever routed through it).
After the flip, LiteLLM stops receiving new traffic — its SpendLogs table simply stops
growing, its rows for the pre-flip period sitting there unchanged — while freerouter starts
accumulating the post-flip rows. Each backend already only ever holds data for the span it
was actually live, so the bill (spend_by_user_and_surface / totals) reads BOTH backends and
merges them, always, rather than switching on GATEWAY_PROVIDER — no cutover timestamp needed,
and no historical LiteLLM row is ever rewritten (read-only seam). `backend()` still selects a
single provider for the things that are inherently about the CURRENT gateway (readiness,
unpriced-model detection), where merging two backends would not mean anything.
"""

from __future__ import annotations

import os

from . import metering, metering_freerouter


def backend():
    if os.environ.get("GATEWAY_PROVIDER", "litellm").strip().lower() == "freerouter":
        return metering_freerouter
    return metering


async def _safe_litellm(coro_fn, *args):
    """LiteLLM's rows, or empty/zeroed if its database is gone.

    Once LiteLLM stops receiving traffic post-flip, a deployment is free to retire its
    database on its own schedule; this seam must keep reading freerouter's half of the
    bill rather than 500 the whole thing because the retired side is unreachable. Mirrors
    the fail-open-to-empty posture metering_freerouter already applies to its own side.
    """
    try:
        return await coro_fn(*args)
    except Exception:
        return None


def _merge_rows(litellm_rows: list[dict] | None, freerouter_rows: list[dict] | None) -> list[dict]:
    """Sum per-(username, surface) rows from both backends into one bill.

    Each backend only ever has rows for the period it was actually live (see module
    docstring), so this is a plain merge-by-key, not a boundary computation — the two sets
    of rows are disjoint in time by construction, and where both happen to name the same
    (username, surface) the spend legitimately adds.
    """
    merged: dict[tuple[str, str], dict] = {}
    for rows in (litellm_rows or [], freerouter_rows or []):
        for r in rows:
            key = (r.get("username"), r.get("surface"))
            acc = merged.setdefault(key, {
                "username": r.get("username"), "surface": r.get("surface"),
                "requests": 0, "spend": 0.0, "prompt_tokens": 0, "completion_tokens": 0,
            })
            acc["requests"] += r.get("requests") or 0
            acc["spend"] += r.get("spend") or 0.0
            acc["prompt_tokens"] += r.get("prompt_tokens") or 0
            acc["completion_tokens"] += r.get("completion_tokens") or 0
    return sorted(merged.values(), key=lambda r: (-r["spend"], r["username"], r["surface"]))


def _merge_totals(litellm_totals: dict | None, freerouter_totals: dict | None) -> dict:
    lt = litellm_totals or {}
    fr = freerouter_totals or {}
    return {
        "requests": (lt.get("requests") or 0) + (fr.get("requests") or 0),
        "spend": (lt.get("spend") or 0.0) + (fr.get("spend") or 0.0),
        "prompt_tokens": (lt.get("prompt_tokens") or 0) + (fr.get("prompt_tokens") or 0),
        "completion_tokens": (lt.get("completion_tokens") or 0) + (fr.get("completion_tokens") or 0),
        "active_keys": (lt.get("active_keys") or 0) + (fr.get("active_keys") or 0),
    }


async def spend_by_user_and_surface(since: str | None = None):
    litellm_rows = await _safe_litellm(metering.spend_by_user_and_surface, since)
    freerouter_rows = await metering_freerouter.spend_by_user_and_surface(since)
    return _merge_rows(litellm_rows, freerouter_rows)


async def totals(since: str | None = None):
    litellm_totals = await _safe_litellm(metering.totals, since)
    freerouter_totals = await metering_freerouter.totals(since)
    return _merge_totals(litellm_totals, freerouter_totals)


async def unpriced_models(since: str | None = None):
    return await backend().unpriced_models(since)


async def ledger_ready() -> bool:
    return await backend().ledger_ready()
