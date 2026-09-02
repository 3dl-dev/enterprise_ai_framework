"""Control plane.

One place that administers identity, virtual keys, budgets and the audit trail across
every surface. The standing constraint is that twelve contracts must never become twelve
consoles — so surfaces are configured from here and never from their own admin UIs.

It is deliberately not in the request path. The gateway serves traffic; this decides who
may, how much, and records what was decided.
"""

import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from . import (
    agent_console,
    agent_usage,
    chat_identity,
    db,
    export,
    freerouter,
    gateway,
    identity,
    issuance,
    metering,
    metering_select,
    mirror,
    portal,
    provisioning,
    workshop,
)
from .analytics import collector as analytics_collector

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


# An OPTIONAL bearer: a browser operator acting through the console carries no token, only
# the proxy's identity headers, so require_operator must not 403 on a missing Authorization.
_optional_bearer = HTTPBearer(auto_error=False)


def require_operator(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_optional_bearer),
) -> str:
    """Authorize an operator WRITE action (item c44).

    Two ways in, both authorising the same actions: the shared admin token (CLI /
    break-glass — unchanged), OR a Keycloak operator ROLE delivered through the
    authenticating proxy (a signed-in operator ACTING in the console). This reverses the
    old token-only posture — operators can now act, not just view — while a plain signed-in
    user still cannot (no operator role → 403). The nuclear actions (exit/revoke-all) stay
    require_admin: too destructive for a browser click.
    """
    token = os.environ.get("CONTROL_PLANE_ADMIN_TOKEN")
    if creds is not None and token and creds.credentials == token:
        return "admin-token"
    if portal.roles(request) & portal.ADMIN_ROLES:
        return portal.require_user(request)  # the operator's username, for the audit trail
    raise HTTPException(403, "operator role or admin token required")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init()
    await db.audit("system", "control_plane.start")
    # When provisioning against the freerouter spoke, the control plane is itself a
    # freerouter tenant (freerouter has no admin key). Bootstrap that tenant once and
    # persist its bearer, so a restart reuses it rather than stranding a new empty tenant.
    # No-op for the LiteLLM default. (item enterpriseaiframework-757)
    if provisioning.backend() is freerouter:
        await freerouter.bootstrap_master_key()
    # Teach app/freerouter to resolve an alias from our OWN mirror record rather than only
    # from freerouter's usage rollup. The rollup is built from recorded spend, so without
    # this a sub-account that has never spent is invisible: `delete_by_aliases` would report
    # a successful revoke having revoked nothing, on both the rotate path and the
    # disabled-in-IdP path. Deliberately here and not at import — after db.init(), so the
    # table it reads exists. (item enterpriseaiframework-1f8)
    mirror.install_alias_resolver()
    # The resident-usage collector (enterpriseaiframework-914). A timer, not a request
    # hook: resident time is measured by sampling a pod that exists, and nobody opens the
    # page while an agent runs overnight. It is a no-op where there is no cluster
    # credential to read pods with, which is every compose deployment.
    collector = agent_usage.start_collector()
    # The behavioural-analytics collector: rebuilds the content-free records store from chat
    # Mongo + each workspace's opencode db + the gateway ledger on a timer. Same no-op
    # discipline — disabled without an in-cluster credential (every compose deployment).
    analytics = analytics_collector.start_collector()
    try:
        yield
    finally:
        await agent_usage.stop_collector(collector)
        await analytics_collector.stop_collector(analytics)


app = FastAPI(title="control-plane", lifespan=lifespan)

# The portal — the user-facing face of this service. Mounted here rather than shipped as a
# separate deployment because the standing constraint is one control plane, not twelve
# consoles, and a portal that was its own service would have been the fifth front door.
# Its routes authenticate from the sidecar proxy; /admin/* below is untouched and still
# requires the shared admin token.
app.include_router(portal.router)
# The workshop, proxied so it can be a tab on this origin instead of a LAN address that
# hangs from any other network. See workshop.py for what replaced the per-pod proxy.
app.include_router(workshop.router)
# An agent's console, ATTACHED — never spawned — at /agents/<name>/ on this same origin.
# A separate module from workshop.py rather than an edit to it: Contract 6 of
# docs/design/records/agents-surface.md freezes the Code surface, the residency semantics
# are the opposite of ttyd's spawn-per-websocket, and the agent port has no NodePort at
# all, so this proxy is the only door to it. See agent_console.py.
app.include_router(agent_console.router)


# ---------------------------------------------------------------- health

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/ready")
async def ready():
    checks = {
        "identity": await identity.health(),
        "gateway": await provisioning.health(),
        "ledger": await metering_select.ledger_ready(),
    }
    return {"ready": all(checks.values()), "checks": checks}


# ---------------------------------------------------------------- identity sync

class SyncResult(BaseModel):
    principals: int
    keys_minted: int
    keys_revoked: int
    details: list[dict] = Field(default_factory=list)


@app.post("/admin/sync", response_model=SyncResult, dependencies=[Depends(require_operator)])
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
            mapping = await provisioning.token_hashes_by_alias()
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
                    await provisioning.delete_by_aliases([r["key_alias"] for r in stale])
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
                created = await provisioning.generate_key(
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


class IssueKeyRequest(BaseModel):
    username: str
    # `chat` | `ide` | `terminal` | `agents/<name>`. The agents form is what
    # deploy/bin/provision-agent.sh asks for, and it is deliberately this endpoint rather
    # than a new one: an agent's key is a virtual key like any other, and a second
    # issuance endpoint would be a second place for the five steps in issuance.py to drift.
    surface: str


class IssuedKey(BaseModel):
    username: str
    surface: str
    key_alias: str
    key: str
    max_budget: float | None = None
    rotated: bool


@app.post("/admin/keys/issue", response_model=IssuedKey,
          dependencies=[Depends(require_operator)])
async def issue_key(req: IssueKeyRequest):
    """Mint a surface key and hand back the raw value exactly once.

    /admin/sync deliberately drops the raw key on the floor — the control plane must not
    become a place worth stealing. That works while every surface is configured by an
    operator who can read the key off the gateway. It does not work for a surface that is
    *provisioned on demand*: the IDE workspace pod has to be handed a live key at the
    moment it is created, and by then nobody holds one.

    The mechanics live in `issuance.issue` because the portal lets a user rotate their
    own key and must do it the identical way; see that module for why each step is there.
    """
    return IssuedKey(**await issuance.issue(req.username, req.surface, actor="admin"))


class BudgetRequest(BaseModel):
    username: str
    surface: str | None = None
    max_budget: float


@app.post("/admin/budget", dependencies=[Depends(require_operator)])
async def set_budget(req: BudgetRequest):
    """Set a hard budget. Past it the gateway refuses, it does not merely record.

    Both backends land the same end state — the live key enforces the new cap and the ledger
    records it — but only one of them can do it in place, and the difference is visible here
    because it has to be:

      * LiteLLM patches the key. The handle does not move, the user's key keeps working, and
        this reads exactly as it always did.
      * freerouter cannot re-cap a key at all (the cap is fixed at mint and there is no
        per-account cap — see freerouter.update_budget). Its `update_budget` MINTS a
        replacement sub-account under the same `<user>::<surface>` alias, and hands back the
        new key plus the handle to retire. The ledger is repointed at the new handle inside
        the same transaction as the budget, and only THEN is the old one revoked. Ordered the
        other way, a failure between the revoke and the commit would leave the user with no
        working key and the control plane with no record of the one that replaced it. Ordered
        this way the worst case is a stranded sub-account, which `/admin/freerouter/reconcile`
        reports and the user's access never stops.

    A rotation is a real change to what the user is holding, so the new keys are returned to
    the operator who asked for the budget change, exactly as `/admin/keys/issue` returns one.
    Silently rotating and saying "updated: 3" would strand every surface the user has open.
    """
    if req.surface and not gateway.is_known_surface(req.surface):
        raise HTTPException(400, f"unknown surface: {req.surface}")

    # One predicate, used by both the select and the update, so they can never drift
    # apart and leave the gateway enforcing a budget the ledger does not record.
    predicate = "p.username = $1 AND k.status = 'active'"
    args: list = [req.username]
    if req.surface:
        predicate += " AND k.surface = $2"
        args.append(req.surface)

    on_freerouter = provisioning.backend() is freerouter

    pool = await db.pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT k.gateway_token_hash, k.surface, k.key_alias, m.account_id
            FROM virtual_key k
            JOIN principal p ON p.id = k.principal_id
            LEFT JOIN freerouter_mirror m ON m.key_alias = k.key_alias
            WHERE {predicate}
            """,
            *args,
        )
        if not rows:
            raise HTTPException(404, "no active keys for that user/surface")

        rotations: list[dict] = []
        for r in rows:
            # On freerouter the ledger's `gateway_token_hash` may still be the LiteLLM hash:
            # the cutover mirror is deliberately additive and never writes over it, so a key
            # that was mirrored rather than re-issued carries the old backend's handle right
            # up until something repoints it. The mirror row is what names the freerouter
            # account for that alias, so it is what the freerouter backend is addressed by —
            # and the rotation below then repoints the ledger, so this converges.
            handle = (
                r["account_id"] if on_freerouter and r["account_id"]
                else r["gateway_token_hash"]
            )
            if not handle:
                continue
            try:
                applied = await provisioning.update_budget(handle, req.max_budget)
            except freerouter.BudgetNotExpressible as exc:
                raise HTTPException(400, str(exc)) from exc
            except KeyError as exc:
                raise HTTPException(
                    409,
                    f"{r['key_alias']}: the gateway has no key this budget can be applied "
                    f"to ({exc}). Re-issue the key (/admin/keys/issue) first.",
                ) from exc
            if isinstance(applied, dict) and applied.get("rotated"):
                rotations.append({"row": r, "applied": applied})

        async with conn.transaction():
            for rot in rotations:
                applied = rot["applied"]
                alias = rot["row"]["key_alias"]
                await conn.execute(
                    "UPDATE virtual_key SET gateway_token_hash = $2 WHERE key_alias = $1",
                    alias, applied["token"],
                )
                await mirror.record_account(
                    conn,
                    key_alias=alias,
                    account_id=applied["token"],
                    key_hash=applied.get("key_hash"),
                    source_max_budget=req.max_budget,
                    limit_usd=applied.get("limit_usd"),
                )
            await conn.execute(
                f"""
                UPDATE virtual_key k SET max_budget = ${len(args) + 1}
                FROM principal p
                WHERE p.id = k.principal_id AND {predicate}
                """,
                *args, req.max_budget,
            )

    # Committed. Now, and not before, the superseded handles stop being able to spend.
    for rot in rotations:
        retire = rot["applied"].get("retire_token")
        if retire:
            await provisioning.revoke_token(retire)

    await db.audit(
        "admin", "budget.set", req.username,
        surface=req.surface or "all", max_budget=req.max_budget,
        # The account ids, never the keys: the audit trail records what was decided, and a
        # bearer in it would make the trail worth stealing.
        rotated=[
            {
                "key_alias": rot["row"]["key_alias"],
                "account_id": rot["applied"]["token"],
                "retired": rot["applied"].get("retire_token"),
            }
            for rot in rotations
        ],
    )
    return {
        "updated": len(rows),
        "max_budget": req.max_budget,
        # Always empty on the LiteLLM backend, which patches in place and never rotates. A
        # non-empty list means the named surfaces are holding a NEW key and their old one has
        # just stopped working, so the operator has to deliver these.
        "rotated": [
            {
                "surface": rot["row"]["surface"],
                "key_alias": rot["row"]["key_alias"],
                "key": rot["applied"]["key"],
            }
            for rot in rotations
        ],
    }


# ------------------------------------------------- the LiteLLM -> freerouter cutover mirror
#
# The gate for the cutover, in the product rather than in an operator's shell history: an
# operator can ask "is it safe to flip GATEWAY_PROVIDER yet?" and get the same answer the
# deploy script's `python -m app.mirror --reconcile` gets, from the same code.


@app.get("/admin/freerouter/reconcile", dependencies=[Depends(require_admin)])
async def freerouter_reconcile(verify_litellm: bool = False):
    """Diff the live LiteLLM key set against the mirrored freerouter sub-accounts.

    `ok` is the flip gate. See app/mirror.reconcile for what each bucket means and, in
    particular, why `unconfirmed` is reported apart from `missing`.
    """
    return await mirror.reconcile(verify_litellm=verify_litellm)


@app.post("/admin/freerouter/mirror", dependencies=[Depends(require_operator)])
async def freerouter_mirror(dry_run: bool = False):
    """Provision the freerouter sub-account for every live LiteLLM key that lacks one.

    Additive and idempotent: it writes only freerouter_mirror rows, so it changes nothing
    about the keys users are holding right now. It requires GATEWAY_PROVIDER=freerouter,
    because it provisions through the selector rather than around it.
    """
    try:
        return await mirror.mirror(dry_run=dry_run)
    except mirror.BackendNotFreerouter as exc:
        raise HTTPException(409, str(exc)) from exc


# ---------------------------------------------------------------- the one bill

@app.get("/admin/spend", dependencies=[Depends(require_admin)])
async def spend(since: str | None = None):
    """Scope item 4. Total spend by user and by surface, all three surfaces, one call.

    This is the bill an operator without a browser has, and it is the query the scope item
    names — so it must name the same people the portal names. It does not translate chat
    identifiers itself: `metering.spend_by_user_and_surface` does that for every reader
    (see chat_identity.attribute). Doing it here as well is what the previous defect
    looked like, one call site at a time.

    Carries its caveats inline. A bill that is quietly missing a model, or quietly showing
    an ObjectId where a name belongs, is worse than one that is obviously broken — so the
    caveat travels with the number rather than living on a page nobody opens.
    """
    rows = await metering_select.spend_by_user_and_surface(since)
    unpriced = await metering_select.unpriced_models(since)
    warnings = []
    if unpriced:
        warnings.append({
            "kind": "unpriced_model",
            "detail": "served traffic but priced at $0 — budgets will not trip "
                      "and this bill under-reports",
            "models": unpriced,
        })
    # A chat principal we could not translate is money we can account for but cannot put a
    # name to. It stays on the bill under a label that says so — dropping it would hide
    # real spend and guessing would be worse — but the operator is told, because the usual
    # cause is the chat database being unreachable from here, which is fixable.
    unresolved = [r for r in rows if chat_identity.is_unresolved(r["username"])]
    if unresolved:
        warnings.append({
            "kind": "unresolved_chat_principal",
            "detail": "chat spend that could not be matched to a person — the chat "
                      "database is unreachable or the account no longer exists; the "
                      "money is still on this bill, only the name is missing",
            "principals": [r["username"] for r in unresolved],
            "requests": sum(r["requests"] for r in unresolved),
            "spend": sum(r["spend"] for r in unresolved),
        })
    return {
        "since": since,
        "totals": await metering_select.totals(since),
        "by_user_and_surface": rows,
        "warnings": warnings,
    }


# ---------------------------------------------------------------- the second dimension


@app.get("/admin/agents/usage", dependencies=[Depends(require_admin)])
async def agent_usage_view(username: str | None = None):
    """Resident time and compute per agent, as USAGE. The browserless half of the portal view.

    A SEPARATE endpoint from `/admin/spend` on purpose. `/admin/spend` is the bill — one
    query, one meaning — and this landing must not change a byte of what it returns. So
    the second dimension is served beside it rather than mixed into it, and a reader who
    wants both asks for both.

    There are no dollars in this payload and there is no rate to configure. Baron ruled
    that owned hardware is metered as usage, not priced: the quantities are hours,
    CPU-core-hours and megabytes. If commodity cloud compute is ever added, wiring a cost
    onto these quantities is a future item — the seam is here, the multiplier is not.
    """
    rows = await agent_usage.usage_by_agent(username)
    warnings = []
    unmeasured = [r for r in rows if not r["compute_measured"]]
    if unmeasured:
        # The honest form of a missing number. A compute figure of 0 with no source is
        # "we could not read the counter", and saying so beats letting it read as idle.
        warnings.append({
            "kind": "compute_not_measured",
            "detail": "resident time is recorded for these agents but no compute counter "
                      "was readable — the cAdvisor scrape is failing, so their "
                      "CPU-core-hours are a floor and not a measurement",
            "agents": [f"{r['user']}/{r['agent']}" for r in unmeasured],
        })
    return {
        "units": {
            "resident": "hours",
            "compute": "cpu-core-hours",
            "memory": "megabytes (peak working set)",
        },
        "collector": {
            "enabled": agent_usage.enabled(),
            "sample_seconds": agent_usage.SAMPLE_SECONDS,
        },
        "agents": rows,
        "warnings": warnings,
    }


@app.post("/admin/agents/usage/collect", dependencies=[Depends(require_operator)])
async def agent_usage_collect():
    """Force one sample. Idempotent in the sense that matters: it can only add elapsed time.

    Exists so an operator (and the live test) can observe the meter move without waiting
    out the timer. It is not a different code path from the timer — it is the same
    `collect_once`, so anything proven through this endpoint is proven about the collector.
    """
    return await agent_usage.collect_once()


@app.get("/admin/unpriced", dependencies=[Depends(require_admin)])
async def unpriced(since: str | None = None):
    """Models consuming tokens at zero recorded cost. Should always be empty."""
    rows = await metering_select.unpriced_models(since)
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


# ---------------------------------------------------------------- the exit path

@app.get("/admin/export/manifest", dependencies=[Depends(require_admin)])
async def export_manifest():
    """Counts and chain head, so a truncated export is detectable."""
    return await export.manifest()


@app.get("/admin/export/audit", dependencies=[Depends(require_admin)])
async def export_audit():
    return StreamingResponse(export.audit_jsonl(), media_type="application/x-ndjson")


@app.get("/admin/export/spend", dependencies=[Depends(require_admin)])
async def export_spend():
    return StreamingResponse(export.spend_csv(), media_type="text/csv")


@app.get("/admin/export/keys", dependencies=[Depends(require_admin)])
async def export_keys():
    return StreamingResponse(export.keys_csv(), media_type="text/csv")


class RevokeAllResult(BaseModel):
    revoked: int
    aliases: list[str] = Field(default_factory=list)


@app.post("/admin/exit/revoke-all", response_model=RevokeAllResult,
          dependencies=[Depends(require_admin)])
async def revoke_all():
    """Delete every virtual key at the gateway.

    The second step of leaving: once the operator's surfaces hold direct provider keys,
    the virtual keys must stop working, or the layer is still a live credential holder
    for traffic nobody is watching any more.
    """
    pool = await db.pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT key_alias FROM virtual_key WHERE status = 'active'"
        )
    aliases = [r["key_alias"] for r in rows]

    # Revoke what the gateway holds too, not only what we recorded — anything minted out
    # of band (the chat surface key is, deliberately) would otherwise survive the exit.
    gateway_aliases = await provisioning.list_aliases()
    all_aliases = sorted(set(aliases) | set(gateway_aliases))

    if all_aliases:
        await provisioning.delete_by_aliases(all_aliases)
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE virtual_key SET status = 'revoked', revoked_at = now() "
                "WHERE status = 'active'"
            )

    await db.audit("admin", "exit.revoke_all", None, count=len(all_aliases))
    return RevokeAllResult(revoked=len(all_aliases), aliases=all_aliases)
