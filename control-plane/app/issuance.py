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

from . import chat_identity, db, gateway, identity, provisioning

# The chat surface's shared key (chat_identity.SHARED_SURFACE_PRINCIPAL, alias
# "chat-surface::chat") is not a person — it is LibreChat's own service credential, minted
# once and held by the surface itself rather than by any signed-in user (see
# chat_identity.py's module docstring). It therefore has no row in the identity provider and
# never will: routing it through `identity.get_user` below would 404 forever. That 404 is
# exactly why every operator script that has ever provisioned it
# (bundle/bin/provision-chat-key.sh, deploy/bin/post-deploy.sh) bypassed `issue` and talked to
# the gateway directly — which left the key with no `virtual_key` row, and therefore invisible
# to the freerouter cutover mirror (app/mirror.py), whose live key set is read straight out of
# that table. A synthetic, permanently-enabled principal closes the gap: `issue("chat-surface",
# "chat", ...)` becomes an ordinary rotate through `provisioning.backend()`, so the shared key
# is mirrored to freerouter before the flip and moves with GATEWAY_PROVIDER like every other
# key, with LibreChat's held credential remapped in place — no user re-login, because no user
# ever held this key to begin with.
_SYNTHETIC_IDP_ID = f"synthetic:{chat_identity.SHARED_SURFACE_PRINCIPAL}"


async def _resolve_principal(conn, username: str) -> dict:
    """The principal row for `username`, reconciling it from the IdP if it is missing.

    The principal table is a mirror of identity, and until now it was refreshed only by a
    full `/admin/sync`. A user added to the IdP after the last sync therefore had no row,
    and every self-service path that mints a key (create an agent, rotate a key) failed
    with an error that told the *end user* to run an operator-only command they cannot run.

    An already-authenticated caller is, by definition, someone the IdP knows: this looks
    them up there and upserts the row on the spot, so self-service works the moment they
    can sign in. Identity stays the source of truth — a username the realm does not know
    still yields no principal (the caller raises 404), and a disabled account is refused —
    so the exist-and-enabled invariant is enforced against identity, not the stale mirror.

    One username is never looked up there at all: chat_identity.SHARED_SURFACE_PRINCIPAL,
    the chat surface's own shared key. See the module-level comment above `_SYNTHETIC_IDP_ID`.
    """
    principal = await conn.fetchrow(
        "SELECT id, idp_user_id, enabled FROM principal WHERE username = $1",
        username,
    )
    if principal is not None:
        return principal

    if username == chat_identity.SHARED_SURFACE_PRINCIPAL:
        return await conn.fetchrow(
            """
            INSERT INTO principal (idp_user_id, username, email, enabled, synced_at)
            VALUES ($1, $2, NULL, TRUE, now())
            ON CONFLICT (idp_user_id) DO UPDATE
                SET username = EXCLUDED.username, enabled = TRUE, synced_at = now()
            RETURNING id, idp_user_id, enabled
            """,
            _SYNTHETIC_IDP_ID, username,
        )

    idp_user = await identity.get_user(username)
    if idp_user is None:
        raise HTTPException(404, f"no such principal: {username}")

    # Upsert on the IdP id, exactly as /admin/sync does, so a later full sync converges to
    # the same row rather than colliding with it.
    return await conn.fetchrow(
        """
        INSERT INTO principal (idp_user_id, username, email, enabled, synced_at)
        VALUES ($1, $2, $3, $4, now())
        ON CONFLICT (idp_user_id) DO UPDATE
            SET username = EXCLUDED.username,
                email    = EXCLUDED.email,
                enabled  = EXCLUDED.enabled,
                synced_at = now()
        RETURNING id, idp_user_id, enabled
        """,
        idp_user["idp_user_id"], idp_user["username"],
        idp_user["email"], idp_user["enabled"],
    )


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
        principal = await _resolve_principal(conn, username)
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
    await provisioning.delete_by_aliases([alias], missing_ok=True)
    created = await provisioning.generate_key(
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
