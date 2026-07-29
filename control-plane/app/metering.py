"""The one bill.

Scope item 4: a single query returns total spend broken down by user and by surface,
across all three surfaces.

The gateway already meters every request it serves and writes a spend row. We do not
duplicate that ledger — we read it and join it to identity through the key alias, which
is the whole reason the alias carries the surface. Reimplementing metering in the
control plane would put us in the data path for no gain.
"""

import os

import asyncpg

from . import chat_identity

_pool: asyncpg.Pool | None = None


async def pool() -> asyncpg.Pool:
    """Read-only-by-convention pool against the gateway's own database."""
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn=os.environ["GATEWAY_DATABASE_URL"], min_size=1, max_size=5
        )
    return _pool


# LiteLLM hashes the key into LiteLLM_SpendLogs.api_key and keeps the alias on
# LiteLLM_VerificationToken.token. The join is on the hashed token.
#
# That join alone is not enough, and the reason is a defect this row hit for real. The
# join is against a table we DELETE from: revoking a disabled user's keys (scope item 6),
# rotating a key when a surface is reprovisioned, and the exit path's revoke-all all
# remove the LiteLLM_VerificationToken row. Every historical spend row for that key then
# joins to NULL and falls out of the bill as "(unattributed)" — observed on the cluster at
# 88% of all spend after a handful of workspace reprovisions.
#
# The bill going quiet about money that was definitely spent is the worst failure this
# component has, so attribution is taken from the alias LiteLLM stamps onto the spend row
# itself at request time, which nothing later deletes. The join survives as a fallback for
# rows written before that metadata existed.
_ALIAS = """COALESCE(
    NULLIF(s.metadata->>'user_api_key_alias', ''),
    NULLIF(v.key_alias, '')
)"""

_LEDGER_JOIN = """
FROM "LiteLLM_SpendLogs" s
LEFT JOIN "LiteLLM_VerificationToken" v ON v.token = s.api_key
"""


# WHICH KEYS MAY NAME SOMEONE OTHER THAN THEMSELVES
#
# `end_user` is whatever the caller put in the request body's "user" field. For a surface
# that serves many people through ONE key it is the only way to tell them apart, and the
# chat surface is exactly that: LibreChat authenticates the person, then forwards them as
# `user`. For a per-user key it is redundant, because the alias already says who holds it.
#
# Trusting it everywhere meant the caller chose the name on the bill. Demonstrated on this
# cluster with a legitimate `baron::ide` key and a body of {"user":"veracity-probe-xyz"}:
# the spend appeared under `veracity-probe-xyz`. The money could not escape the key's own
# budget — caps bind to the key — but attribution is the product, and attribution was
# forgeable by anybody holding any key.
#
# So end_user is honoured only for keys minted AS shared surfaces, and ignored everywhere
# else in favour of the alias. Defaults to the one shared key we mint (provision-chat-key.sh
# and post-deploy.sh both use this alias); override for a deployment that adds another.
#
# Failing closed is the point: an alias that is absent, deleted, or simply not on this list
# falls through to the alias-derived name, which the holder cannot choose.
SHARED_SURFACE_ALIASES = [
    a.strip() for a in os.environ.get(
        "SHARED_SURFACE_ALIASES", "chat-surface::chat"
    ).split(",") if a.strip()
]

# Only a key on that list gets to speak for someone else.
_TRUSTED_END_USER = f"""CASE
    WHEN {_ALIAS} = ANY($SHARED::text[]) THEN NULLIF(s.end_user, '')
    ELSE NULL
END"""


async def spend_by_user_and_surface(since: str | None = None) -> list[dict]:
    """The single query the scope item names. One row per (principal, surface).

    The principal is named here, by `chat_identity.attribute`, and not by the caller.
    The chat surface identifies people by LibreChat's internal ObjectId, so the raw
    column is hex for the surface most people use; translating it in each renderer is
    what let `/admin/spend` and the portal disagree about who spent the money
    (finding 34). Every reader of the one bill now gets the same names by construction.
    """
    where, params = "", []
    if since:
        where = 'WHERE s."startTime" >= $1::text::timestamptz'
        params.append(since)

    # Attribution precedence: the end user a SHARED surface forwarded, then the user
    # encoded in the key alias. See SHARED_SURFACE_ALIASES — a per-user key naming
    # somebody else is ignored, because otherwise the caller picks who gets billed.
    params.append(SHARED_SURFACE_ALIASES)
    trusted = _TRUSTED_END_USER.replace("$SHARED", f"${len(params)}")
    sql = f"""
    SELECT
        COALESCE(
            {trusted},
            NULLIF(split_part({_ALIAS}, '::', 1), ''),
            '(unattributed)'
        ) AS username,
        COALESCE(NULLIF(split_part({_ALIAS}, '::', 2), ''), '(unknown)') AS surface,
        COUNT(*)                                  AS requests,
        COALESCE(SUM(s.spend), 0)::float8         AS spend,
        COALESCE(SUM(s.prompt_tokens), 0)::bigint AS prompt_tokens,
        COALESCE(SUM(s.completion_tokens), 0)::bigint AS completion_tokens
    {_LEDGER_JOIN}
    {where}
    GROUP BY 1, 2
    ORDER BY spend DESC, username, surface
    """
    p = await pool()
    async with p.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return chat_identity.attribute([dict(r) for r in rows])


async def totals(since: str | None = None) -> dict:
    where, params = "", []
    if since:
        where = 'WHERE s."startTime" >= $1::text::timestamptz'
        params.append(since)
    sql = f"""
    SELECT COUNT(*) AS requests,
           COALESCE(SUM(s.spend), 0)::float8 AS spend,
           COUNT(DISTINCT {_ALIAS}) AS active_keys
    {_LEDGER_JOIN}
    {where}
    """
    p = await pool()
    async with p.acquire() as conn:
        row = await conn.fetchrow(sql, *params)
    return dict(row)


async def unpriced_models(since: str | None = None) -> list[dict]:
    """Models that consumed tokens but recorded no spend.

    A model absent from the gateway's price map still serves traffic and still counts
    tokens — it just prices every request at zero. Budgets therefore never trip and the
    bill silently under-reports. Nothing errors, which is what makes it dangerous.

    This is the leak detector (design §2.5) reduced to the one case this row can check
    without invoice reconciliation.
    """
    # Cache hits are excluded: they cost nothing upstream, so $0 against counted tokens is
    # correct rather than a missing price. Counting them here made the detector fire on
    # healthy traffic, and a detector that cries wolf is one people stop reading — which
    # is exactly how a genuinely unpriced model would then slip through.
    where = "WHERE s.total_tokens > 0 AND lower(COALESCE(s.cache_hit, '')) <> 'true'"
    params: list = []
    if since:
        where += ' AND s."startTime" >= $1::text::timestamptz'
        params.append(since)
    sql = f"""
    SELECT s.model,
           COUNT(*)                                  AS requests,
           COALESCE(SUM(s.total_tokens), 0)::bigint  AS tokens,
           COALESCE(SUM(s.spend), 0)::float8         AS spend
    FROM "LiteLLM_SpendLogs" s
    {where}
    GROUP BY s.model
    HAVING COALESCE(SUM(s.spend), 0) = 0
    ORDER BY tokens DESC
    """
    p = await pool()
    async with p.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return [dict(r) for r in rows]


async def ledger_ready() -> bool:
    """True once the gateway has created its tables. Used by readiness, not liveness."""
    try:
        p = await pool()
        async with p.acquire() as conn:
            return bool(await conn.fetchval("SELECT to_regclass('public.\"LiteLLM_SpendLogs\"')"))
    except Exception:
        return False
