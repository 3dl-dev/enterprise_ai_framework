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


def _split_alias(name: str, account_id: str) -> tuple[str, str]:
    """A sub-account's label is "<user>::<surface>" (set by the control plane at mint).

    Fall back to the account_id as the username with an unknown surface when a row carries
    no label (e.g. the control-plane tenant's own account, or a pre-1da account).
    """
    if "::" in name:
        user, surface = name.split("::", 1)
        return user, surface
    return (name or account_id or "(unknown)"), "(unknown)"


async def _rollup(since: str | None) -> list[dict]:
    """Operator subtree usage grouped by sub-account, via freerouter-573.

    GET /api/v1/usage/rollup (requireTenant, subtree-scoped to the control-plane tenant) →
    {data:[{account_id, name, spend_micro, input_tokens, output_tokens, request_count}]}.
    Mapped to metering.spend_by_user_and_surface's shape. Empty on any failure (unreachable
    router, malformed body) so the console renders a blank-but-honest bill rather than
    erroring.
    """
    try:
        params = {"since": since} if since else None
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{base_url()}/api/v1/usage/rollup", headers=_headers(), params=params
            )
            resp.raise_for_status()
            data = resp.json().get("data", [])
    except Exception:
        return []
    rows: list[dict] = []
    for a in data:
        if not isinstance(a, dict):
            continue
        user, surface = _split_alias(a.get("name") or "", a.get("account_id") or "")
        rows.append({
            "username": user,
            "surface": surface,
            "requests": a.get("request_count", 0) or 0,
            "spend": (a.get("spend_micro", 0) or 0) / 1_000_000,  # micro-USD → USD
            "prompt_tokens": a.get("input_tokens", 0) or 0,
            "completion_tokens": a.get("output_tokens", 0) or 0,
        })
    return rows


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
