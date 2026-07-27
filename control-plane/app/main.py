"""Control plane.

One place that administers identity, virtual keys, budgets and the audit trail across
every surface. The standing constraint is that twelve contracts must never become twelve
consoles — so surfaces are configured from here and never from their own admin UIs.

It is deliberately not in the request path. The gateway serves traffic; this decides who
may, how much, and records what was decided.
"""

import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from . import db, gateway, identity, metering

bearer = HTTPBearer(auto_error=True)


def require_admin(creds: HTTPAuthorizationCredentials = Depends(bearer)) -> str:
    """Admin API auth.

    Single shared admin token for the dogfood row. This is a known gap, recorded as
    such: it is not the OIDC-delegated admin auth the design calls for, and it must not
    survive past the dogfood scope.
    """
    expected = os.environ.get("CONTROL_PLANE_ADMIN_TOKEN")
    if not expected:
        raise HTTPException(500, "CONTROL_PLANE_ADMIN_TOKEN is not configured")
    if creds.credentials != expected:
        raise HTTPException(401, "bad admin token")
    return "admin"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init()
    await db.audit("system", "control_plane.start")
    yield


app = FastAPI(title="control-plane", lifespan=lifespan)


# ---------------------------------------------------------------- health

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/ready")
async def ready():
    checks = {
        "identity": await identity.health(),
        "gateway": await gateway.health(),
        "ledger": await metering.ledger_ready(),
    }
    return {"ready": all(checks.values()), "checks": checks}


# ---------------------------------------------------------------- identity sync

class SyncResult(BaseModel):
    principals: int
    keys_minted: int
    keys_revoked: int
    details: list[dict] = Field(default_factory=list)


@app.post("/admin/sync", response_model=SyncResult, dependencies=[Depends(require_admin)])
async def sync(default_budget: float | None = Query(default=None)):
    """Reconcile identity → virtual keys.

    Idempotent by construction: it computes the difference between what identity says
    should exist and what the gateway actually holds, then closes the gap. Running it
    twice changes nothing the second time, which is what makes it safe to run on a timer.
    """
    users = await identity.list_users()
    pool = await db.pool()
    minted = revoked = 0
    details: list[dict] = []

    # Backfill any active key whose token hash we do not know. This happens after the
    # migration that purged raw keys, and it must not require re-minting keys that are
    # in active use on someone's laptop.
    async with pool.acquire() as conn:
        unknown = await conn.fetch(
            "SELECT key_alias FROM virtual_key WHERE status = 'active' AND gateway_token_hash IS NULL"
        )
        if unknown:
            mapping = await gateway.token_hashes_by_alias()
            for r in unknown:
                token = mapping.get(r["key_alias"])
                if token:
                    await conn.execute(
                        "UPDATE virtual_key SET gateway_token_hash = $2 WHERE key_alias = $1",
                        r["key_alias"], token,
                    )

    async with pool.acquire() as conn:
        for u in users:
            row = await conn.fetchrow(
                """
                INSERT INTO principal (idp_user_id, username, email, enabled, synced_at)
                VALUES ($1, $2, $3, $4, now())
                ON CONFLICT (idp_user_id) DO UPDATE
                    SET username = EXCLUDED.username,
                        email    = EXCLUDED.email,
                        enabled  = EXCLUDED.enabled,
                        synced_at = now()
                RETURNING id, enabled
                """,
                u["idp_user_id"], u["username"], u["email"], u["enabled"],
            )
            principal_id = row["id"]

            if not u["enabled"]:
                # Disabled in the IdP: pull every surface key. This is scope item 6.
                stale = await conn.fetch(
                    "SELECT key_alias FROM virtual_key "
                    "WHERE principal_id = $1 AND status = 'active'",
                    principal_id,
                )
                if stale:
                    await gateway.delete_by_aliases([r["key_alias"] for r in stale])
                    await conn.execute(
                        "UPDATE virtual_key SET status = 'revoked', revoked_at = now() "
                        "WHERE principal_id = $1 AND status = 'active'",
                        principal_id,
                    )
                    revoked += len(stale)
                    await db.audit(
                        "system", "key.revoke_all", u["username"],
                        reason="disabled_in_idp", count=len(stale),
                    )
                    details.append({"user": u["username"], "action": "revoked", "n": len(stale)})
                continue

            for surface in gateway.SURFACES:
                existing = await conn.fetchval(
                    "SELECT 1 FROM virtual_key "
                    "WHERE principal_id = $1 AND surface = $2 AND status = 'active'",
                    principal_id, surface,
                )
                if existing:
                    continue
                created = await gateway.generate_key(
                    username=u["username"],
                    surface=surface,
                    idp_user_id=u["idp_user_id"],
                    max_budget=default_budget,
                )
                await conn.execute(
                    """
                    INSERT INTO virtual_key
                        (principal_id, surface, key_alias, gateway_token_hash, max_budget, status)
                    VALUES ($1, $2, $3, $4, $5, 'active')
                    ON CONFLICT (principal_id, surface) DO UPDATE
                        SET gateway_token_hash = EXCLUDED.gateway_token_hash,
                            status = 'active',
                            revoked_at = NULL,
                            max_budget = EXCLUDED.max_budget
                    """,
                    principal_id, surface,
                    gateway.key_alias(u["username"], surface),
                    # The hash, not created["key"]. The raw key is returned to us here
                    # and deliberately dropped on the floor — the operator collects it
                    # from the gateway, and we never become a place worth stealing.
                    created.get("token"), default_budget,
                )
                minted += 1
                await db.audit(
                    "system", "key.mint", u["username"],
                    surface=surface, max_budget=default_budget,
                )
                details.append({"user": u["username"], "action": "minted", "surface": surface})

    return SyncResult(
        principals=len(users), keys_minted=minted, keys_revoked=revoked, details=details
    )


# ---------------------------------------------------------------- keys

@app.get("/admin/keys", dependencies=[Depends(require_admin)])
async def list_keys(username: str | None = None):
    pool = await db.pool()
    sql = """
        SELECT p.username, k.surface, k.key_alias, k.status, k.max_budget, k.created_at
        FROM virtual_key k JOIN principal p ON p.id = k.principal_id
    """
    args = []
    if username:
        sql += " WHERE p.username = $1"
        args.append(username)
    sql += " ORDER BY p.username, k.surface"
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)
    return [dict(r) for r in rows]


class BudgetRequest(BaseModel):
    username: str
    surface: str | None = None
    max_budget: float


@app.post("/admin/budget", dependencies=[Depends(require_admin)])
async def set_budget(req: BudgetRequest):
    """Set a hard budget. Past it the gateway refuses, it does not merely record."""
    if req.surface and req.surface not in gateway.SURFACES:
        raise HTTPException(400, f"unknown surface: {req.surface}")

    # One predicate, used by both the select and the update, so they can never drift
    # apart and leave the gateway enforcing a budget the ledger does not record.
    predicate = "p.username = $1 AND k.status = 'active'"
    args: list = [req.username]
    if req.surface:
        predicate += " AND k.surface = $2"
        args.append(req.surface)

    pool = await db.pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch(
                f"""
                SELECT k.gateway_token_hash, k.surface
                FROM virtual_key k JOIN principal p ON p.id = k.principal_id
                WHERE {predicate}
                """,
                *args,
            )
            if not rows:
                raise HTTPException(404, "no active keys for that user/surface")
            for r in rows:
                if r["gateway_token_hash"]:
                    await gateway.update_budget(r["gateway_token_hash"], req.max_budget)
            await conn.execute(
                f"""
                UPDATE virtual_key k SET max_budget = ${len(args) + 1}
                FROM principal p
                WHERE p.id = k.principal_id AND {predicate}
                """,
                *args, req.max_budget,
            )
    await db.audit(
        "admin", "budget.set", req.username,
        surface=req.surface or "all", max_budget=req.max_budget,
    )
    return {"updated": len(rows), "max_budget": req.max_budget}


# ---------------------------------------------------------------- the one bill

@app.get("/admin/spend", dependencies=[Depends(require_admin)])
async def spend(since: str | None = None):
    """Scope item 4. Total spend by user and by surface, all three surfaces, one call.

    Carries the unpriced-model warning inline. A bill that is quietly missing a model is
    worse than one that is obviously broken, so the caveat travels with the number rather
    than living on a page nobody opens.
    """
    unpriced = await metering.unpriced_models(since)
    return {
        "since": since,
        "totals": await metering.totals(since),
        "by_user_and_surface": await metering.spend_by_user_and_surface(since),
        "warnings": (
            [
                {
                    "kind": "unpriced_model",
                    "detail": "served traffic but priced at $0 — budgets will not trip "
                              "and this bill under-reports",
                    "models": unpriced,
                }
            ]
            if unpriced
            else []
        ),
    }


@app.get("/admin/unpriced", dependencies=[Depends(require_admin)])
async def unpriced(since: str | None = None):
    """Models consuming tokens at zero recorded cost. Should always be empty."""
    rows = await metering.unpriced_models(since)
    return {"ok": not rows, "models": rows}


# ---------------------------------------------------------------- the one audit trail

@app.get("/admin/audit", dependencies=[Depends(require_admin)])
async def audit_log(limit: int = Query(default=100, le=1000)):
    pool = await db.pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT seq, ts, actor, action, target, detail, hash "
            "FROM audit_event ORDER BY seq DESC LIMIT $1",
            limit,
        )
    return [dict(r) for r in rows]


@app.get("/admin/audit/verify", dependencies=[Depends(require_admin)])
async def audit_verify():
    """Recompute the hash chain end to end. An append-only claim nobody checks is decoration."""
    return await db.verify_chain()
