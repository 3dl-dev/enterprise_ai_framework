"""freerouter provisioning — the inference-spoke analogue of app/gateway.py.

The control plane provisions keys against the bundled **freerouter** spoke instead of
LiteLLM. It is signature-compatible with app/gateway.py so the same callers (agents.py,
the portal, the sync loop) drive either backend, selected by GATEWAY_PROVIDER. Design:
docs/design/records/freerouter-reference-router.md (C1/C3), item enterpriseaiframework-757.

The one model difference from LiteLLM, and how it is bridged here:

  LiteLLM has a single master key that mints aliased keys. freerouter has no admin scope
  (`requireTenant` resolves a bearer to a *tenant*); a tenant mints *sub-keys under itself*
  (`MintSubKey`). So the control plane is itself ONE freerouter tenant — provisioned once at
  bootstrap via `POST /api/v1/signup`, its one-time bearer stored as FREEROUTER_MASTER_KEY
  (the GATEWAY_MASTER_KEY analogue) — and every `<user>::<surface>` key is a sub-key minted
  under that tenant, attributed by the sub-key's `name`. Per-user *nested* tenancy (a tenant
  per user, item enterpriseaiframework-1da) layers on top later; this flat mapping is the
  drop-in that retires LiteLLM's provisioning without changing the caller contract.

Confirmed live against freerouter (signup=open):
  POST /api/v1/signup {display_name} -> {data:{account_id, parent_account_id, api_key}}
  POST /api/v1/keys   {name, limit}  -> {data:{hash, name, label, limit, ...}, key}
  GET  /api/v1/keys                  -> {data:[{hash, name, label, limit, usage, ...}]}
  PATCH/DELETE /api/v1/keys/{hash}
"""

from __future__ import annotations

import os

import httpx

# Alias grammar is provider-agnostic — reuse it so both backends attribute identically.
from .gateway import surface_alias


def base_url() -> str:
    return os.environ.get("FREEROUTER_URL", "http://freerouter:8080").rstrip("/")


def _headers() -> dict:
    """The control plane's own tenant bearer — freerouter's GATEWAY_MASTER_KEY analogue.

    Bootstrapped once (a single POST /api/v1/signup at deploy time) and injected as a
    secret, exactly as GATEWAY_MASTER_KEY is. All sub-keys are minted under this tenant.
    """
    return {"Authorization": f"Bearer {os.environ['FREEROUTER_MASTER_KEY']}"}


async def _request(method: str, path: str, **kwargs) -> httpx.Response:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.request(
            method, f"{base_url()}{path}", headers=_headers(), **kwargs
        )
        resp.raise_for_status()
        return resp


async def generate_key(
    *, username: str, surface: str, idp_user_id: str, max_budget: float | None
) -> dict:
    """Mint a sub-key bound to one user and one surface, under the control-plane tenant.

    Returns freerouter's create envelope: {"data": {"hash", "name", "label", "limit", ...},
    "key": "<raw one-time key>"}. `idp_user_id` is retained in the signature for
    gateway.py parity; freerouter attributes by the sub-key `name`, so it rides in `name`.
    """
    body: dict = {"name": surface_alias(username, surface)}
    if max_budget is not None:
        # freerouter's `limit` is a monthly USD ceiling (limitToMonthlyUSD server-side).
        body["limit"] = max_budget
    resp = await _request("POST", "/api/v1/keys", json=body)
    return resp.json()


async def delete_by_aliases(aliases: list[str], *, missing_ok: bool = False) -> dict:
    """Hard-revoke by alias. freerouter deletes by hash, so resolve alias -> hash first.

    Mirrors gateway.delete_by_aliases including the missing_ok contract: a rotation that
    finds nothing to delete is a valid outcome, not an error.
    """
    if not aliases:
        return {"deleted_keys": []}
    by_alias = await token_hashes_by_alias()
    wanted = [(a, by_alias[a]) for a in aliases if a in by_alias]
    if not wanted:
        if missing_ok:
            return {"deleted_keys": []}
        raise KeyError(f"no freerouter key matches aliases {aliases}")
    deleted: list[str] = []
    for alias, key_hash in wanted:
        await _request("DELETE", f"/api/v1/keys/{key_hash}")
        deleted.append(alias)
    return {"deleted_keys": deleted}


async def update_budget(token_hash: str, max_budget: float) -> dict:
    """Update a sub-key's monthly USD ceiling in place, without re-minting.

    NOTE: freerouter's PATCH currently mutates only `name` and `disabled`
    (internal/core/keys.go PatchKeyRequest) — NOT `limit`. Until it does (freerouter ask,
    tracked from enterpriseaiframework-757), an in-place budget change cannot be applied:
    delete+re-mint would rotate the user's key and drop accrued usage, which is exactly
    what gateway.update_budget exists to avoid. We send `limit` (forward-compatible so this
    starts working the moment freerouter ships the field) and VERIFY it took effect, raising
    loudly rather than silently leaving the old budget in force.
    """
    await _request("PATCH", f"/api/v1/keys/{token_hash}", json={"limit": max_budget})
    for k in await list_keys():
        if k.get("hash") == token_hash:
            if k.get("limit") != max_budget:
                raise NotImplementedError(
                    "freerouter PATCH /api/v1/keys/{hash} does not update `limit` yet "
                    "(only name/disabled); in-place budget change is a pending freerouter "
                    "ask — see enterpriseaiframework-757"
                )
            return k
    raise KeyError(f"no freerouter key with hash {token_hash}")


async def list_keys() -> list[dict]:
    """Every sub-key under the control-plane tenant."""
    resp = await _request("GET", "/api/v1/keys")
    data = resp.json().get("data", [])
    return [k for k in data if isinstance(k, dict)]


async def token_hashes_by_alias() -> dict[str, str]:
    """alias (sub-key name) -> hash, as freerouter currently holds it.

    freerouter's list carries the durable `hash`; the sub-key `name` is the alias the
    control plane assigned. Unnamed keys (e.g. the tenant's own bearer key) are skipped.
    """
    return {
        k["name"]: k["hash"]
        for k in await list_keys()
        if k.get("name") and k.get("hash")
    }


async def list_aliases(prefix: str | None = None) -> list[str]:
    """Aliases (sub-key names) freerouter currently holds. Used to verify revocation."""
    names = [k["name"] for k in await list_keys() if k.get("name")]
    return [a for a in names if not prefix or a.startswith(prefix)]


async def health() -> bool:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{base_url()}/healthz")
            return r.status_code == 200
    except Exception:
        return False


async def ensure_operator_tenant() -> str:
    """Return the control plane's freerouter tenant bearer, provisioning it once if absent.

    freerouter has no admin key: the control plane is itself a tenant under op-root, created
    by a one-time `POST /api/v1/signup` (requires FREEROUTER_SIGNUP=open on the spoke). When
    FREEROUTER_MASTER_KEY is already set (a durable restart, or an injected secret) it is
    returned unchanged; otherwise a tenant is provisioned and its one-time bearer returned
    for the caller to PERSIST — signing up twice would strand a second empty tenant, so the
    caller must store the result (secret / control-plane DB) rather than call this per boot.
    """
    existing = os.environ.get("FREEROUTER_MASTER_KEY", "").strip()
    if existing:
        return existing
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{base_url()}/api/v1/signup",
            json={"display_name": "enterprise-ai-control-plane"},
        )
        resp.raise_for_status()
        return resp.json()["data"]["api_key"]
