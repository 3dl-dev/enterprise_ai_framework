"""Storage for the owner-scoped agent-manager token (agents-raven.md, Contract F).

A CONTROL-PLANE credential, not a gateway key. It authorizes `/agent-manager/v1/*` calls as
one owner, on behalf of one Raven, and nothing else: the gateway never learns it exists, so
it cannot run inference. It therefore lives in its own table with its own verifier rather
than in `virtual_key` (a spend ledger) — a gateway-side leak must not become an
infrastructure-control leak.

Only the SHA-256 of the token is stored. The plaintext exists in exactly one place: the
Raven's own `agent-<owner>-<raven>-key` Secret, as `EAF_AGENT_MANAGER_TOKEN`. No endpoint
returns it, including to the owner.

This module holds no HTTP logic and imports nothing from `agents.py`, so `agents.create`
and `agents.delete` can mint and revoke without an import cycle. The verifier and the
router live in `agent_manager.py`.
"""

from __future__ import annotations

import base64
import hashlib
import secrets

from . import db

TOKEN_PREFIX = "eafam_"


def mint_plaintext() -> str:
    """`eafam_` + 32 random bytes, base64url, unpadded."""
    raw = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")
    return TOKEN_PREFIX + raw


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def issue(owner: str, raven_name: str, *, actor: str) -> str:
    """Mint the ONE live token for (owner, raven) and return its plaintext.

    Any earlier live row for the same pair is revoked in the same transaction, so a Raven
    re-created under a name whose previous incarnation was not cleanly deleted never ends up
    with two live credentials (the partial UNIQUE index would refuse the insert anyway).
    """
    token = mint_plaintext()
    pool = await db.pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "UPDATE agent_manager_token SET revoked_at = now() "
                "WHERE owner = $1 AND raven_name = $2 AND revoked_at IS NULL",
                owner, raven_name,
            )
            await conn.execute(
                "INSERT INTO agent_manager_token (token_hash, owner, raven_name) "
                "VALUES ($1, $2, $3)",
                token_hash(token), owner, raven_name,
            )
    await db.audit(actor, "agent-manager.issue", f"{owner}/{raven_name}",
                   owner=owner, raven=raven_name, hash_prefix=token_hash(token)[:12])
    return token


async def revoke(owner: str, raven_name: str, *, actor: str, reason: str) -> int:
    """Revoke the live token(s) of one Raven. Returns how many rows were revoked."""
    pool = await db.pool()
    async with pool.acquire() as conn:
        status = await conn.execute(
            "UPDATE agent_manager_token SET revoked_at = now() "
            "WHERE owner = $1 AND raven_name = $2 AND revoked_at IS NULL",
            owner, raven_name,
        )
    n = _rowcount(status)
    if n:
        await db.audit(actor, "agent-manager.revoke", f"{owner}/{raven_name}",
                       owner=owner, raven=raven_name, reason=reason, count=n)
    return n


async def revoke_owner(conn, owner: str) -> int:
    """Revoke EVERY live token of one owner on an open connection (the IdP-disable sync)."""
    status = await conn.execute(
        "UPDATE agent_manager_token SET revoked_at = now() "
        "WHERE owner = $1 AND revoked_at IS NULL",
        owner,
    )
    return _rowcount(status)


async def lookup_live(token: str) -> dict | None:
    """The live row for a presented bearer, or None. Unknown and revoked look identical."""
    pool = await db.pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            # bool_and: principal.username is not unique (idp_user_id is), so every row
            # carrying the name must be enabled; NULL (no principal at all) is refused too.
            "SELECT t.owner, t.raven_name, "
            "  (SELECT bool_and(p.enabled) FROM principal p WHERE p.username = t.owner) "
            "    AS owner_enabled "
            "FROM agent_manager_token t "
            "WHERE t.token_hash = $1 AND t.revoked_at IS NULL",
            token_hash(token),
        )
    return dict(row) if row is not None else None


async def touch(token: str) -> None:
    pool = await db.pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE agent_manager_token SET last_used_at = now() "
            "WHERE token_hash = $1 AND revoked_at IS NULL",
            token_hash(token),
        )


def _rowcount(status: str) -> int:
    # asyncpg returns the command tag, e.g. "UPDATE 2".
    try:
        return int(str(status).rsplit(" ", 1)[-1])
    except ValueError:
        return 0
