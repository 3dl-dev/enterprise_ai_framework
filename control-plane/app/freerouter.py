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
from pathlib import Path

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
    """Mint a nested sub-ACCOUNT bound to one user and one surface (item 1da).

    Each (user,surface) is its OWN sub-account under the control-plane tenant, not a sub-key,
    so the operator bill (freerouter-573 rollup) attributes per (user,surface) by AccountID.
    POST /api/v1/subaccounts {name:"<user>::<surface>"} → {data:{account_id, api_key, name}}.

    Returns the caller contract gateway.generate_key satisfies: `token` = the durable handle
    the control plane stores (here the ACCOUNT_ID), `key` = the one-time bearer handed to the
    surface. `max_budget` is not enforced per-account in M1 — every sub-account draws on the
    operator tab (freerouter-171); per-account caps are a later capability (ca9). `idp_user_id`
    rides for gateway.py parity; attribution is by the sub-account label.
    """
    resp = await _request(
        "POST", "/api/v1/subaccounts", json={"name": surface_alias(username, surface)}
    )
    data = resp.json()["data"]
    return {"token": data["account_id"], "key": data["api_key"], "data": data}


async def _account_ids_by_alias() -> dict[str, str]:
    """alias "<user>::<surface>" → sub-account id, from the operator subtree rollup (573).

    The rollup lists sub-accounts that have accrued usage. The control plane's own DB is the
    authoritative alias→account_id map (stored at mint); this is the freerouter-side view,
    used to resolve a revoke target by alias. A never-used sub-account has no rollup row yet —
    the control plane revokes such by passing its stored account_id directly.
    """
    resp = await _request("GET", "/api/v1/usage/rollup")
    return {
        a["name"]: a["account_id"]
        for a in resp.json().get("data", [])
        if a.get("name") and a.get("account_id")
    }


async def delete_by_aliases(aliases: list[str], *, missing_ok: bool = False) -> dict:
    """Hard-revoke each (user,surface) by alias, parent-scoped.

    DELETE /api/v1/subaccounts/{account_id} disables every key under the sub-account so it
    can't authenticate — ledger/bill row intact, and the control plane never holds the user's
    key (posture preserved). missing_ok mirrors gateway: nothing to revoke is a valid outcome.
    """
    if not aliases:
        return {"deleted_keys": []}
    by_alias = await _account_ids_by_alias()
    wanted = [(a, by_alias[a]) for a in aliases if a in by_alias]
    if not wanted:
        if missing_ok:
            return {"deleted_keys": []}
        raise KeyError(f"no freerouter sub-account matches aliases {aliases}")
    deleted: list[str] = []
    for alias, account_id in wanted:
        await _request("DELETE", f"/api/v1/subaccounts/{account_id}")
        deleted.append(alias)
    return {"deleted_keys": deleted}


async def update_budget(token_hash: str, max_budget: float) -> dict:
    """Per-account budget is not an M1 feature: every sub-account draws on the operator tab
    (freerouter-171), and there is no per-account cap endpoint yet (ca9). Fail loudly rather
    than silently pretend a per-user budget was applied."""
    raise NotImplementedError(
        "per-(user,surface) budget is not supported on the freerouter path yet — M1 uses the "
        "operator tab (freerouter-171); per-account caps are pending (ca9). See "
        "enterpriseaiframework-1da"
    )


async def list_keys() -> list[dict]:
    """The control plane's sub-accounts as the operator subtree rollup reports them."""
    resp = await _request("GET", "/api/v1/usage/rollup")
    return [a for a in resp.json().get("data", []) if isinstance(a, dict)]


async def token_hashes_by_alias() -> dict[str, str]:
    """alias "<user>::<surface>" → sub-account id (the durable handle the control plane stores
    at mint). Freerouter-side view, used to verify/backfill."""
    return await _account_ids_by_alias()


async def list_aliases(prefix: str | None = None) -> list[str]:
    """Sub-account aliases the operator subtree currently shows. Used to verify revocation."""
    names = list((await _account_ids_by_alias()).keys())
    return [a for a in names if not prefix or a.startswith(prefix)]


async def health() -> bool:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{base_url()}/healthz")
            return r.status_code == 200
    except Exception:
        return False


async def _signup_operator_tenant() -> str:
    """Provision the control plane's own freerouter tenant, returning its one-time bearer.

    freerouter has no admin key: the control plane is itself a tenant under op-root, created
    by a one-time `POST /api/v1/signup` (requires FREEROUTER_SIGNUP=open on the spoke).
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{base_url()}/api/v1/signup",
            json={"display_name": "enterprise-ai-control-plane"},
        )
        resp.raise_for_status()
        return resp.json()["data"]["api_key"]


def keyfile_path() -> str | None:
    p = os.environ.get("FREEROUTER_MASTER_KEY_FILE", "").strip()
    return p or None


async def bootstrap_master_key() -> str:
    """Resolve the control plane's freerouter bearer, provisioning + persisting it once.

    Precedence: an injected FREEROUTER_MASTER_KEY secret wins; else a previously-persisted
    keyfile (FREEROUTER_MASTER_KEY_FILE) is reused; else the tenant is signed up ONCE and the
    one-time bearer is written to the keyfile so it survives restarts — signing up twice would
    strand a second empty tenant. The resolved key is exported into the process env so the
    rest of this module (`_headers`) uses it unchanged.

    Idempotent across restarts as long as either the secret or the keyfile persists; call it
    once at startup when GATEWAY_PROVIDER=freerouter.
    """
    existing = os.environ.get("FREEROUTER_MASTER_KEY", "").strip()
    if existing:
        return existing

    path = keyfile_path()
    if path:
        try:
            stored = Path(path).read_text().strip()
        except FileNotFoundError:
            stored = ""
        if stored:
            os.environ["FREEROUTER_MASTER_KEY"] = stored
            return stored

    bearer = await _signup_operator_tenant()
    if path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(bearer)
        p.chmod(0o600)
    os.environ["FREEROUTER_MASTER_KEY"] = bearer
    return bearer
