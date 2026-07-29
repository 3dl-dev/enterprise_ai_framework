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

from . import chat_identity, db, metering

# Secrets never leave in an export, and the token hash is not useful to the operator
# afterwards, so the key inventory carries identity and lifecycle only.
KEY_COLUMNS = ("username", "surface", "key_alias", "status", "max_budget", "created_at", "revoked_at")

# RAW AND RESOLVED, BOTH, DELIBERATELY.
#
# `end_user` is exactly what the caller put in the request body — for the chat surface,
# LibreChat's internal ObjectId. It stays verbatim because this file is evidence: it is
# what an operator reconciles against a provider invoice after this platform is deleted,
# and a column that has been "helpfully" rewritten cannot be reconciled against anything.
#
# `principal` is who that row is billed to, by the same rule and the same SQL the one bill
# uses (metering.ledger_attribution_sql), with a chat ObjectId translated to a username.
# Without it the archive a departing customer keeps is a column of hex for the surface
# most people use, and it would disagree with the bill they were shown while they were a
# customer — which is finding 34 all over again, in the one rendering that outlives us.
#
# `status` is here for the same reason `cache_hit` is, and was added when the aggregate
# bill learned to say the same thing (enterpriseaiframework-e69). This CSV is per-request,
# so it has no `requests` count to explain — but it does hand the customer $0 rows, and
# without `status` a row that the provider failed is indistinguishable from one that was
# served. `cache_hit` alone cannot tell them apart: an upstream failure carries
# cache_hit='False' and $0, exactly like a request that has not been priced. This is the
# third rendering of the ledger, and the two before it each had to be corrected twice
# because a fix landed in one and was forgotten in the others (see
# metering.ledger_attribution_sql).
SPEND_COLUMNS = (
    "request_id", "start_time", "end_time", "model", "key_alias", "surface",
    "end_user", "principal", "status", "spend", "prompt_tokens",
    "completion_tokens", "total_tokens", "cache_hit",
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


def spend_row(record) -> list:
    """One ledger row as CSV values. Pure apart from the already-primed identity cache.

    Split out from the query so the naming half can be tested without a database — the
    half that decides what a human reads is the half that was wrong twice.
    """
    r = dict(record)
    # `refresh_on_miss=False`: the map is loaded once for the whole export by spend_csv.
    # Left to itself resolve() re-reads the chat database on every miss, and an export of
    # a tenant whose chat accounts are gone is nothing but misses.
    r["principal"] = chat_identity.resolve(r.get("principal") or "", refresh_on_miss=False)
    return [r[c] for c in SPEND_COLUMNS]


async def spend_csv() -> AsyncIterator[str]:
    """Every metered request, attributed by the same rule as the one bill.

    THE ORDER THE EXIT PATH RUNS IN IS WHY THIS MATTERS. `exit.sh full` revokes every
    virtual key, and revocation DELETES from LiteLLM_VerificationToken. A rendering that
    attributes through that table is therefore at its emptiest exactly when it is the only
    record left — measured on the cluster as 265 of 477 exported rows with no principal at
    all, over a ledger the bill attributed all but 42 of. So attribution comes from the
    alias LiteLLM stamps onto the spend row itself, which nothing later deletes, with the
    key-table join kept only as a fallback for rows written before that metadata existed.
    """
    yield _csv_line(SPEND_COLUMNS)

    # Load the chat id -> username map once for the whole file rather than per row.
    chat_identity.refresh()

    params = [metering.SHARED_SURFACE_ALIASES]
    attr = metering.ledger_attribution_sql(f"${len(params)}")
    sql = f"""
        SELECT s.request_id,
               s."startTime"::text AS start_time,
               s."endTime"::text   AS end_time,
               s.model,
               COALESCE({attr["alias"]}, '')   AS key_alias,
               COALESCE({attr["surface"]}, '') AS surface,
               COALESCE(s.end_user, '')        AS end_user,
               {attr["principal"]}             AS principal,
               COALESCE(s.status, '')          AS status,
               s.spend, s.prompt_tokens, s.completion_tokens, s.total_tokens,
               COALESCE(s.cache_hit, '')       AS cache_hit
        {attr["join"]}
        ORDER BY s."startTime" ASC
    """
    pool = await metering.pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            cursor = conn.cursor(sql, *params)
            async for r in cursor:
                yield _csv_line(spend_row(r))


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
            "spend.csv": (
                "one row per metered request; `end_user` is the raw request field as sent, "
                "`principal` is who it is billed to, by the same rule as the bill"
            ),
            "keys.csv": "virtual key inventory; contains no secrets",
        },
        "verify": "bundle/bin/verify-export.py <export-dir>",
    }
