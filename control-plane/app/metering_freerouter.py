"""freerouter-backed usage reads — the read-side analogue of metering.py (LiteLLM).

Same interface as app/metering.py (spend_by_user_and_surface / totals / unpriced_models /
ledger_ready); selected at the flip by app/metering_select. Design record C3: the operator
bill reads freerouter usage, unified in and out of EAF.

The per-(user,surface) bill needs freerouter's OPERATOR SUBTREE ROLLUP (freerouter-573):
freerouter records AccountID, not the sub-key hash, and meter.Usage is scoped to one
account, so EAF cannot assemble a subtree bill client-side. Until 573 lands, the spend
reads return EMPTY rather than the stale LiteLLM numbers — an honestly-blank bill on the new
backend is correct, where reading LiteLLM_SpendLogs after the flip would be wrong. When 573
lands, its rows (keyed by AccountID = <user>::<surface>, with spend_micro / input_tokens /
output_tokens / request_count) drop straight into `_rollup`.
"""

from __future__ import annotations

import os

import httpx

from . import chat_identity


def base_url() -> str:
    return os.environ.get("FREEROUTER_URL", "http://freerouter:8080").rstrip("/")


def _headers() -> dict:
    return {"Authorization": f"Bearer {os.environ['FREEROUTER_MASTER_KEY']}"}


async def _rollup(since: str | None) -> list[dict]:
    """Operator subtree usage grouped by sub-account, via freerouter-573.

    Returns rows shaped like metering.spend_by_user_and_surface's:
    {username, surface, requests, spend, prompt_tokens, completion_tokens}. Empty until the
    573 endpoint exists (or if the router is unreachable) — never raises, so the console
    renders a blank-but-honest bill rather than erroring.
    """
    # TODO(freerouter-573): call the operator subtree rollup once its path/schema is
    # confirmed, e.g. GET /v1/usage/subtree, and map AccountID -> (username, surface) by
    # splitting on "::". Until then, degrade to empty.
    return []


async def spend_by_user_and_surface(since: str | None = None) -> list[dict]:
    rows = await _rollup(since)
    # Same identity-normalization the LiteLLM path applies, so both backends name people
    # the same way (finding 34) even though attribution here is by sub-account.
    return chat_identity.attribute(rows)


async def totals(since: str | None = None) -> dict:
    rows = await _rollup(since)
    return {
        "requests": sum(r.get("requests", 0) for r in rows),
        "spend": sum(r.get("spend", 0.0) for r in rows),
        "prompt_tokens": sum(r.get("prompt_tokens", 0) for r in rows),
        "completion_tokens": sum(r.get("completion_tokens", 0) for r in rows),
    }


async def unpriced_models(since: str | None = None) -> list[dict]:
    # LiteLLM's $0-spend leak detector is moot on freerouter: its price-equality invariant
    # ties the displayed /v1/models price to what the meter bills, so an unpriced-but-served
    # model cannot exist. Nothing to flag.
    return []


async def ledger_ready() -> bool:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{base_url()}/healthz")
            return r.status_code == 200
    except Exception:
        return False
