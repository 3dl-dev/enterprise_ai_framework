"""Gateway (LiteLLM) admin client.

We drive the gateway's key lifecycle rather than reimplementing key validation —
the gateway is the sole token validator and the single budget admission point
(design §7.2), so the control plane's job is to keep its key set in agreement with
identity, not to sit in the data path.

MIT core only. Nothing here touches the `enterprise/` feature set.
"""

import os

import httpx

SURFACES = ("chat", "ide", "terminal")


def base_url() -> str:
    return os.environ.get("GATEWAY_URL", "http://gateway:4000").rstrip("/")


def _headers() -> dict:
    return {"Authorization": f"Bearer {os.environ['GATEWAY_MASTER_KEY']}"}


def key_alias(username: str, surface: str) -> str:
    """Alias encodes the surface so spend attribution is a join, not a guess."""
    if surface not in SURFACES:
        raise ValueError(f"unknown surface: {surface}")
    return f"{username}::{surface}"


def parse_alias(alias: str) -> tuple[str, str] | None:
    if "::" not in alias:
        return None
    username, _, surface = alias.rpartition("::")
    if surface not in SURFACES:
        return None
    return username, surface


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
    """Mint a virtual key bound to one user and one surface.

    The key is minted against the operator's own upstream credentials, which live only
    in the gateway config. No surface ever holds a provider key (scope item 2).
    """
    body: dict = {
        "key_alias": key_alias(username, surface),
        "user_id": idp_user_id,
        "metadata": {"surface": surface, "username": username, "issuer": "control-plane"},
    }
    if max_budget is not None:
        body["max_budget"] = max_budget
    resp = await _request("POST", "/key/generate", json=body)
    return resp.json()


async def delete_by_aliases(aliases: list[str]) -> dict:
    """Hard-revoke by alias. Used when identity says the principal is gone or disabled.

    Deleting by alias rather than by key value is what lets the control plane avoid
    holding the raw virtual keys at all.
    """
    if not aliases:
        return {"deleted_keys": []}
    resp = await _request("POST", "/key/delete", json={"key_aliases": aliases})
    return resp.json()


async def update_budget(token_hash: str, max_budget: float) -> dict:
    """The gateway accepts its own token hash wherever it documents `key`."""
    resp = await _request(
        "POST", "/key/update", json={"key": token_hash, "max_budget": max_budget}
    )
    return resp.json()


# The gateway rejects size > 100. Asking for more returns a validation error, not a
# truncated page, so an unpaginated read silently yields nothing at all.
_PAGE_SIZE = 100
_MAX_PAGES = 100


async def list_keys() -> list[dict]:
    """Every key the gateway holds, across all pages."""
    out: list[dict] = []
    for page in range(1, _MAX_PAGES + 1):
        resp = await _request(
            "GET",
            "/key/list",
            params={"return_full_object": "true", "page": page, "size": _PAGE_SIZE},
        )
        batch = [k for k in resp.json().get("keys", []) if isinstance(k, dict)]
        out.extend(batch)
        if len(batch) < _PAGE_SIZE:
            break
    return out


async def token_hashes_by_alias() -> dict[str, str]:
    """alias -> token hash, as the gateway currently holds it.

    Used to backfill rows whose hash is unknown, so budget updates keep working without
    revoking and re-minting keys that are in active use.
    """
    return {
        k["key_alias"]: k["token"]
        for k in await list_keys()
        if k.get("key_alias") and k.get("token")
    }


async def list_aliases(prefix: str | None = None) -> list[str]:
    """Aliases the gateway currently holds. Used to verify revocation gateway-side."""
    aliases = [k["key_alias"] for k in await list_keys() if k.get("key_alias")]
    return [a for a in aliases if not prefix or a.startswith(prefix)]


async def health() -> bool:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{base_url()}/health/liveliness")
            return r.status_code == 200
    except Exception:
        return False
