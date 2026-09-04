"""Gateway (LiteLLM) admin client.

We drive the gateway's key lifecycle rather than reimplementing key validation —
the gateway is the sole token validator and the single budget admission point
(design §7.2), so the control plane's job is to keep its key set in agreement with
identity, not to sit in the data path.

MIT core only. Nothing here touches the `enterprise/` feature set.
"""

import os
import re

import httpx

SURFACES = ("chat", "ide", "terminal")

# ---------------------------------------------------------------- the agents surface
#
# Contract 1 of docs/design/records/agents-surface.md, and the whole of it. A user has ONE
# chat/ide/terminal but MANY agents, so the alias has to carry an instance discriminator —
# and the obvious spelling for that, `<user>::agents::<name>`, is the losing one:
#
#   `parse_alias` below splits on the LAST "::" (rpartition), while metering.py's SQL
#   splits on the FIRST (split_part(alias,'::',1|2)). With two fields they agree. With
#   three they disagree — Python reads the username as `alice::agents`, SQL reads the
#   surface as `agents` and loses the instance entirely. Both renderers wrong, differently.
#
# So the instance is folded into the SURFACE field with a "/" and the alias keeps exactly
# one "::":
#
#   <username>::agents/<name>            e.g. alice::agents/scraper
#
# rpartition then yields ("alice", "agents/scraper") and split_part yields the same two
# strings, so `metering.spend_by_user_and_surface` attributes an agent's inference to the
# right user under a per-instance surface WITH NO QUERY CHANGE AT ALL. Everything below is
# additive: `key_alias` and its `surface in SURFACES` guard are untouched, so no existing
# chat/ide/terminal alias changes by one byte.
AGENT_SURFACE = "agents"

# The SAME slug the workspace enforces on project names and provision-agent.sh enforces on
# agent names (deploy/workspace/shell-server.py: SLUG_OK). It is load-bearing rather than
# cosmetic: it guarantees <name> contains neither "::" nor "/", which is what keeps the
# alias to one separator and makes the round-trip above exact.
AGENT_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,38}$")


def agent_surface(name: str) -> str:
    """The surface field for one agent instance: `agents/<name>`."""
    if not AGENT_SLUG.match(name or ""):
        raise ValueError(
            f"agent name must match {AGENT_SLUG.pattern}: {name!r}. Constrained rather "
            "than sanitised — a rejected name is easy to explain, a silently rewritten "
            "one is not, and a name carrying '/' or '::' would break the alias grammar."
        )
    return f"{AGENT_SURFACE}/{name}"


def agent_instance(surface: str) -> str | None:
    """The `<name>` of an agent-instance surface, or None if this is not one.

    The membership test for the agents family. It is deliberately not
    `surface.startswith("agents/")`: `agents/` with an empty or malformed name would then
    be accepted as a surface and mint an alias nothing can attribute.
    """
    prefix = AGENT_SURFACE + "/"
    if not surface or not surface.startswith(prefix):
        return None
    name = surface[len(prefix):]
    return name if AGENT_SLUG.match(name) else None


def agent_key_alias(username: str, name: str) -> str:
    """Contract 1's alias for one agent instance. Never routed through `key_alias`."""
    return f"{username}::{agent_surface(name)}"


def is_known_surface(surface: str) -> bool:
    """Base surface or agent instance. The one predicate callers should use."""
    return surface in SURFACES or agent_instance(surface) is not None


def surface_alias(username: str, surface: str) -> str:
    """The alias for any surface this deployment knows about.

    For chat/ide/terminal it returns exactly what `key_alias` returns, by calling it —
    there is no second spelling of a base-surface alias anywhere.
    """
    if surface in SURFACES:
        return key_alias(username, surface)
    name = agent_instance(surface)
    if name is None:
        raise ValueError(f"unknown surface: {surface}")
    return agent_key_alias(username, name)


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
    # ONE added clause (`is_known_surface` in place of `in SURFACES`); every existing
    # chat/ide/terminal alias parses to exactly the tuple it parsed to before.
    if "::" not in alias:
        return None
    username, _, surface = alias.rpartition("::")
    if not is_known_surface(surface):
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
        # `surface_alias`, not `key_alias`, so this one mint path serves an agent instance
        # too. For chat/ide/terminal it IS `key_alias` — same call, same string.
        "key_alias": surface_alias(username, surface),
        "user_id": idp_user_id,
        "metadata": {"surface": surface, "username": username, "issuer": "control-plane"},
    }
    if max_budget is not None:
        body["max_budget"] = max_budget
    resp = await _request("POST", "/key/generate", json=body)
    return resp.json()


async def delete_by_aliases(aliases: list[str], *, missing_ok: bool = False) -> dict:
    """Hard-revoke by alias. Used when identity says the principal is gone or disabled.

    Deleting by alias rather than by key value is what lets the control plane avoid
    holding the raw virtual keys at all.

    The gateway answers 404 when none of the aliases exist. For revocation that is worth
    surfacing — it means our ledger and the gateway disagree about what exists. For a
    rotation it is not: "there was nothing to delete" is a perfectly good outcome, and
    treating it as an error makes issuing the FIRST key for a surface fail. Callers say
    which case they are in rather than every caller learning this the hard way.
    """
    if not aliases:
        return {"deleted_keys": []}
    try:
        resp = await _request("POST", "/key/delete", json={"key_aliases": aliases})
    except httpx.HTTPStatusError as exc:
        if missing_ok and exc.response.status_code == 404:
            return {"deleted_keys": []}
        raise
    return resp.json()


async def update_budget(token_hash: str, max_budget: float) -> dict:
    """The gateway accepts its own token hash wherever it documents `key`.

    LiteLLM patches the cap ON the existing key, so the key the user is holding keeps working
    and its handle does not move. The result therefore carries no `rotated` marker, and
    main.set_budget's continuity branch (which exists for freerouter, whose caps are fixed at
    mint) stays untaken on this backend.
    """
    resp = await _request(
        "POST", "/key/update", json={"key": token_hash, "max_budget": max_budget}
    )
    return resp.json()


async def revoke_token(token: str, *, missing_ok: bool = True) -> dict:
    """Hard-revoke ONE key by its token hash rather than by alias.

    The provisioning interface's counterpart to freerouter.revoke_token: retire a handle the
    ledger has already replaced. Unreachable on this backend today — `update_budget` above
    patches in place and so never asks anything to be retired — and it is here because the
    seam is what callers program against, not the backend that happens to be selected.
    """
    if not token:
        return {"deleted_keys": []}
    try:
        resp = await _request("POST", "/key/delete", json={"keys": [token]})
    except httpx.HTTPStatusError as exc:
        if missing_ok and exc.response.status_code == 404:
            return {"deleted_keys": []}
        raise
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
