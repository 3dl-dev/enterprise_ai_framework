"""Minting a virtual key, in one place.

This was the body of `POST /admin/keys/issue`. It moved here when the portal gained a
"rotate my key" button, because the alternative was a second implementation of the same
five steps — and the steps are not decorative. Each one exists because of a specific
failure:

  * the principal must exist and be ENABLED, or a disabled account gets handed a
    spendable key, which is the exact thing the disable is for;
  * the previous key for that (principal, surface) is deleted BEFORE the new one exists,
    so a leaked manifest cannot outlive a reprovision and two live keys never make spend
    attribution a guess;
  * the ledger's recorded token hash is updated in the same transaction, or budget
    changes later fail against a key the gateway no longer has — silently, from the
    operator's side;
  * an existing budget is carried across the rotation, or rotating a key quietly removes
    the cap that was the reason for setting it;
  * the whole thing is audited.

A second copy of that list would have drifted from this one within a release.
"""

from fastapi import HTTPException

from . import db, gateway


async def issue(username: str, surface: str, *, actor: str) -> dict:
    """Rotate (or first-mint) one principal's key for one surface.

    `actor` is who is recorded as having done it — "admin" for the operator API, the
    user's own name when they rotate their own from the portal. The PRINCIPAL is always
    `username`; there is deliberately no way to mint a key for somebody else here.
    """
    # `is_known_surface`, so `agents/<name>` mints here too rather than through a parallel
    # copy of the five steps above. That was the whole reason this module exists: an agent
    # key minted anywhere else would skip the enabled check, or leave the ledger's token
    # hash pointing at a key the gateway no longer holds. See gateway.AGENT_SURFACE for
    # the alias grammar and why the instance rides in the surface field.
    if not gateway.is_known_surface(surface):
        raise HTTPException(400, f"unknown surface: {surface}")

    pool = await db.pool()
    async with pool.acquire() as conn:
        principal = await conn.fetchrow(
            "SELECT id, idp_user_id, enabled FROM principal WHERE username = $1",
            username,
        )
        if principal is None:
            raise HTTPException(404, f"no such principal: {username} (run /admin/sync)")
        if not principal["enabled"]:
            raise HTTPException(409, f"{username} is disabled in the identity provider")

        existing = await conn.fetchrow(
            "SELECT max_budget, status FROM virtual_key "
            "WHERE principal_id = $1 AND surface = $2",
            principal["id"], surface,
        )

    alias = gateway.surface_alias(username, surface)
    max_budget = (
        float(existing["max_budget"])
        if existing and existing["max_budget"] is not None
        else None
    )

    # missing_ok: there may be no key to rotate. That is the normal state the first time a
    # surface is provisioned and also the state after a revocation, so treating "nothing
    # to delete" as an error would fail in the two cases this is most needed.
    await gateway.delete_by_aliases([alias], missing_ok=True)
    created = await gateway.generate_key(
        username=username, surface=surface,
        idp_user_id=principal["idp_user_id"], max_budget=max_budget,
    )

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO virtual_key
                (principal_id, surface, key_alias, gateway_token_hash, max_budget, status)
            VALUES ($1, $2, $3, $4, $5, 'active')
            ON CONFLICT (principal_id, surface) DO UPDATE
                SET gateway_token_hash = EXCLUDED.gateway_token_hash,
                    key_alias = EXCLUDED.key_alias,
                    status = 'active',
                    revoked_at = NULL,
                    max_budget = EXCLUDED.max_budget
            """,
            principal["id"], surface, alias, created.get("token"), max_budget,
        )

    await db.audit(
        actor, "key.issue", username,
        surface=surface, rotated=existing is not None, max_budget=max_budget,
    )
    return {
        "username": username,
        "surface": surface,
        "key_alias": alias,
        "key": created["key"],
        "max_budget": max_budget,
        "rotated": existing is not None,
    }
