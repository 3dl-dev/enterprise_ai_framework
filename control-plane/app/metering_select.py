"""Usage-read backend selector — LiteLLM (metering) or freerouter (metering_freerouter).

The read-side twin of app/provisioning: the operator bill / portal / analytics read through
here, and a single GATEWAY_PROVIDER flip repoints them from LiteLLM's SpendLogs to
freerouter's usage without touching any caller. Default 'litellm' through the transition, so
switching every call site to this module changes no behaviour until the flip. item 6cc.
"""

from __future__ import annotations

import os

from . import metering, metering_freerouter


def backend():
    if os.environ.get("GATEWAY_PROVIDER", "litellm").strip().lower() == "freerouter":
        return metering_freerouter
    return metering


async def spend_by_user_and_surface(since: str | None = None):
    return await backend().spend_by_user_and_surface(since)


async def totals(since: str | None = None):
    return await backend().totals(since)


async def unpriced_models(since: str | None = None):
    return await backend().unpriced_models(since)


async def ledger_ready() -> bool:
    return await backend().ledger_ready()
