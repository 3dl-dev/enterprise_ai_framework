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
_LEDGER_JOIN = """
FROM "LiteLLM_SpendLogs" s
LEFT JOIN "LiteLLM_VerificationToken" v ON v.token = s.api_key
"""


async def spend_by_user_and_surface(since: str | None = None) -> list[dict]:
    """The single query the scope item names. One row per (user, surface)."""
    where, params = "", []
    if since:
        where = 'WHERE s."startTime" >= $1::text::timestamptz'
        params.append(since)

    # Attribution precedence: the end user the surface forwarded, then the user encoded
    # in the key alias. A surface that serves many people through one shared virtual key
    # (the chat surface does) is only distinguishable via end_user, while a per-user key
    # (the coding agents) carries it in the alias. Preferring end_user makes both work
    # without the caller needing to know which kind of surface it is looking at.
    sql = f"""
    SELECT
        COALESCE(
            NULLIF(s.end_user, ''),
            NULLIF(split_part(v.key_alias, '::', 1), ''),
            '(unattributed)'
        ) AS username,
        COALESCE(NULLIF(split_part(v.key_alias, '::', 2), ''), '(unknown)') AS surface,
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
    return [dict(r) for r in rows]


async def totals(since: str | None = None) -> dict:
    where, params = "", []
    if since:
        where = 'WHERE s."startTime" >= $1::text::timestamptz'
        params.append(since)
    sql = f"""
    SELECT COUNT(*) AS requests,
           COALESCE(SUM(s.spend), 0)::float8 AS spend,
           COUNT(DISTINCT v.key_alias) AS active_keys
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
    where, params = 'WHERE s.total_tokens > 0', []
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
