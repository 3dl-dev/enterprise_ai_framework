"""Ledger export — the anti-lock-in mechanism.

Scope item 9. The operator must be able to leave with everything the layer accumulated on
their behalf, in formats nothing here is needed to read.

Two properties do the work:

- **Complete.** Spend, audit trail and key inventory, with a manifest recording counts so
  a truncated export is detectable rather than merely smaller.
- **Independently verifiable.** The audit chain is exported with the hashes intact, so it
  can be re-verified from the file alone after this software is deleted. An archive you
  need the vendor's running system to trust is not an exit.

Formats are CSV and JSONL on purpose. No proprietary container, nothing that needs this
codebase to parse.
"""

import csv
import io
import json
from typing import AsyncIterator

from . import db, metering

# Secrets never leave in an export, and the token hash is not useful to the operator
# afterwards, so the key inventory carries identity and lifecycle only.
KEY_COLUMNS = ("username", "surface", "key_alias", "status", "max_budget", "created_at", "revoked_at")

SPEND_COLUMNS = (
    "request_id", "start_time", "end_time", "model", "key_alias", "surface",
    "end_user", "spend", "prompt_tokens", "completion_tokens", "total_tokens", "cache_hit",
)


def _csv_line(values) -> str:
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerow(values)
    return buf.getvalue()


async def audit_jsonl() -> AsyncIterator[str]:
    """The full audit trail, oldest first, hashes intact.

    Order matters: the chain is only verifiable in the order it was written.
    """
    pool = await db.pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            cursor = conn.cursor(
                "SELECT seq, ts::text AS ts, actor, action, target, detail, prev_hash, hash "
                "FROM audit_event ORDER BY seq ASC"
            )
            async for r in cursor:
                detail = r["detail"]
                if isinstance(detail, str):
                    detail = json.loads(detail)
                yield json.dumps(
                    {
                        "seq": r["seq"],
                        "ts": r["ts"],
                        "actor": r["actor"],
                        "action": r["action"],
                        "target": r["target"],
                        "detail": detail,
                        "prev_hash": r["prev_hash"],
                        "hash": r["hash"],
                    },
                    sort_keys=True,
                ) + "\n"


async def spend_csv() -> AsyncIterator[str]:
    """Every metered request, joined to the key alias that produced it."""
    yield _csv_line(SPEND_COLUMNS)
    pool = await metering.pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            cursor = conn.cursor(
                """
                SELECT s.request_id,
                       s."startTime"::text AS start_time,
                       s."endTime"::text   AS end_time,
                       s.model,
                       COALESCE(v.key_alias, '')                        AS key_alias,
                       COALESCE(NULLIF(split_part(v.key_alias, '::', 2), ''), '') AS surface,
                       COALESCE(s.end_user, '')                         AS end_user,
                       s.spend, s.prompt_tokens, s.completion_tokens, s.total_tokens,
                       COALESCE(s.cache_hit, '')                        AS cache_hit
                FROM "LiteLLM_SpendLogs" s
                LEFT JOIN "LiteLLM_VerificationToken" v ON v.token = s.api_key
                ORDER BY s."startTime" ASC
                """
            )
            async for r in cursor:
                yield _csv_line([r[c] for c in SPEND_COLUMNS])


async def keys_csv() -> AsyncIterator[str]:
    yield _csv_line(KEY_COLUMNS)
    pool = await db.pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT p.username, k.surface, k.key_alias, k.status, k.max_budget,
                   k.created_at::text AS created_at,
                   COALESCE(k.revoked_at::text, '') AS revoked_at
            FROM virtual_key k JOIN principal p ON p.id = k.principal_id
            ORDER BY p.username, k.surface
            """
        )
    for r in rows:
        yield _csv_line([r[c] for c in KEY_COLUMNS])


async def manifest() -> dict:
    """Counts and the chain head, so a truncated export is detectable.

    Without this, a partial export looks like a smaller one.
    """
    cp_pool = await db.pool()
    async with cp_pool.acquire() as conn:
        audit_count = await conn.fetchval("SELECT count(*) FROM audit_event")
        chain_head = await conn.fetchval(
            "SELECT hash FROM audit_event ORDER BY seq DESC LIMIT 1"
        )
        key_count = await conn.fetchval("SELECT count(*) FROM virtual_key")
        principal_count = await conn.fetchval("SELECT count(*) FROM principal")

    gw_pool = await metering.pool()
    async with gw_pool.acquire() as conn:
        spend_rows = await conn.fetchval('SELECT count(*) FROM "LiteLLM_SpendLogs"')
        total_spend = await conn.fetchval(
            'SELECT COALESCE(SUM(spend), 0)::float8 FROM "LiteLLM_SpendLogs"'
        )

    return {
        "audit_events": audit_count,
        "audit_chain_head": chain_head or db.GENESIS_HASH,
        "virtual_keys": key_count,
        "principals": principal_count,
        "spend_rows": spend_rows,
        "total_spend": total_spend,
        "formats": {
            "audit.jsonl": "one JSON object per line, oldest first, hash-chained",
            "spend.csv": "one row per metered request",
            "keys.csv": "virtual key inventory; contains no secrets",
        },
        "verify": "bundle/bin/verify-export.py <export-dir>",
    }
