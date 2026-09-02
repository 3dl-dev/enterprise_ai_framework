"""Provisioning backend selector — the LiteLLM gateway or the freerouter spoke.

Both app/gateway.py and app/freerouter.py expose the same provisioning interface
(generate_key / delete_by_aliases / update_budget / revoke_token / list_keys /
token_hashes_by_alias / list_aliases / health). Callers import from HERE and never branch on
the backend; which one is live is a single env decision, read at call time so a flip needs no
restart of the caller's import graph.

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
    """Apply a new cap to one key handle.

    The two backends reach the same end state by different means and the RESULT says which:
    LiteLLM patches the live key and returns its own key object; freerouter cannot re-cap a
    key at all, so it mints a replacement sub-account under the same alias and returns
    `rotated: True` with `key`, `token` (the new handle) and `retire_token` (the old one, for
    `revoke_token` once the ledger has been updated). A caller that ignores the result gets
    the LiteLLM behaviour it always had and, on freerouter, a correctly capped key that the
    ledger has stopped pointing at — so the result is not optional on the write path.
    """
    return await backend().update_budget(token_hash, max_budget)


async def revoke_token(token: str, *, missing_ok: bool = True):
    """Retire ONE gateway handle by id — the second half of a rotation, never the first.

    Addressed by handle rather than by alias because after a rotation two handles have worn
    the alias and only the caller knows which one the ledger has just replaced.
    """
    return await backend().revoke_token(token, missing_ok=missing_ok)


async def list_keys():
    return await backend().list_keys()


async def token_hashes_by_alias():
    return await backend().token_hashes_by_alias()


async def list_aliases(prefix: str | None = None):
    return await backend().list_aliases(prefix)


async def health() -> bool:
    return await backend().health()
