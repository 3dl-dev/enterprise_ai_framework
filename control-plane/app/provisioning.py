"""Provisioning backend selector — the LiteLLM gateway or the freerouter spoke.

Both app/gateway.py and app/freerouter.py expose the same provisioning interface
(generate_key / delete_by_aliases / update_budget / list_keys / token_hashes_by_alias /
list_aliases / health). Callers import from HERE and never branch on the backend; which one
is live is a single env decision, read at call time so a flip needs no restart of the
caller's import graph.

GATEWAY_PROVIDER selects the backend. Default 'litellm' through the transition (design
record, transition step 2); flips to 'freerouter' once the spoke is the gateway. item
enterpriseaiframework-757.
"""

from __future__ import annotations

import os

from . import freerouter, gateway


def backend():
    """The selected provisioning module (gateway or freerouter), by GATEWAY_PROVIDER."""
    if os.environ.get("GATEWAY_PROVIDER", "litellm").strip().lower() == "freerouter":
        return freerouter
    return gateway


async def generate_key(*, username: str, surface: str, idp_user_id: str, max_budget):
    return await backend().generate_key(
        username=username, surface=surface, idp_user_id=idp_user_id, max_budget=max_budget
    )


async def delete_by_aliases(aliases, *, missing_ok: bool = False):
    return await backend().delete_by_aliases(aliases, missing_ok=missing_ok)


async def update_budget(token_hash: str, max_budget: float):
    return await backend().update_budget(token_hash, max_budget)


async def list_keys():
    return await backend().list_keys()


async def token_hashes_by_alias():
    return await backend().token_hashes_by_alias()


async def list_aliases(prefix: str | None = None):
    return await backend().list_aliases(prefix)


async def health() -> bool:
    return await backend().health()
