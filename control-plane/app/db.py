"""Control-plane state and the hash-chained audit trail.

Postgres is welded (design §7.2) — this is not a swap port, so the schema is written
directly rather than through an ORM abstraction that buys portability we do not want.
"""

import hashlib
import json
import os

import asyncpg

_pool: asyncpg.Pool | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS principal (
    id           BIGSERIAL PRIMARY KEY,
    idp_user_id  TEXT UNIQUE NOT NULL,
    username     TEXT NOT NULL,
    email        TEXT,
    enabled      BOOLEAN NOT NULL DEFAULT TRUE,
    first_seen   TIMESTAMPTZ NOT NULL DEFAULT now(),
    synced_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One virtual key per (principal, surface). Surface is carried by the key rather than
-- inferred from the request, which is what makes spend-by-surface a single query
-- instead of a heuristic (scope item 4).
CREATE TABLE IF NOT EXISTS virtual_key (
    id            BIGSERIAL PRIMARY KEY,
    principal_id  BIGINT NOT NULL REFERENCES principal(id) ON DELETE CASCADE,
    -- 'chat' | 'ide' | 'terminal' | 'agents/<name>'. A user has ONE of each base surface
    -- and MANY agents, so an agent's instance rides IN the surface field — see
    -- gateway.AGENT_SURFACE for why that, and not a third '::' field, is the grammar.
    -- UNIQUE (principal_id, surface) below therefore still means "one key per thing you
    -- can spend from", per agent rather than per agent family.
    surface       TEXT NOT NULL CHECK (
        surface IN ('chat', 'ide', 'terminal')
        OR surface ~ '^agents/[a-z0-9][a-z0-9-]{0,38}$'
    ),
    key_alias     TEXT UNIQUE NOT NULL,
    -- The gateway's SHA-256 of the virtual key, never the key itself. It is the join
    -- column against the spend ledger, and it is not a credential — possessing it does
    -- not let you call the gateway.
    gateway_token_hash TEXT,
    status        TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'revoked')),
    max_budget    NUMERIC(12, 4),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at    TIMESTAMPTZ,
    UNIQUE (principal_id, surface)
);

-- Append-only, hash-chained (design §8.4). Tamper is detectable by recomputing the
-- chain; there is no UPDATE or DELETE path in the application.
CREATE TABLE IF NOT EXISTS audit_event (
    seq        BIGSERIAL PRIMARY KEY,
    ts         TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor      TEXT NOT NULL,
    action     TEXT NOT NULL,
    target     TEXT,
    detail     JSONB NOT NULL DEFAULT '{}'::jsonb,
    prev_hash  TEXT NOT NULL,
    hash       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS audit_event_ts_idx ON audit_event (ts);
CREATE INDEX IF NOT EXISTS virtual_key_principal_idx ON virtual_key (principal_id);

-- An earlier build of this column stored the raw sk- virtual key. Rename it and purge
-- any raw key left behind, so an existing deployment does not keep a live credential at
-- rest after upgrading.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'virtual_key' AND column_name = 'gateway_token_id'
    ) THEN
        ALTER TABLE virtual_key RENAME COLUMN gateway_token_id TO gateway_token_hash;
    END IF;
END $$;

UPDATE virtual_key SET gateway_token_hash = NULL WHERE gateway_token_hash LIKE 'sk-%';

-- The agents surface, for a database created before it existed. CREATE TABLE IF NOT
-- EXISTS above is a no-op on an existing deployment, so the widened CHECK it carries
-- would never reach one — and the symptom would be an integrity error at the moment an
-- operator provisions their first agent, which reads as "the agents surface is broken".
-- Drop-then-add rather than ADD IF NOT EXISTS (Postgres has no such form for CHECK), and
-- the constraint is named explicitly so this is idempotent on every boot: re-adding the
-- identical predicate under the identical name.
ALTER TABLE virtual_key DROP CONSTRAINT IF EXISTS virtual_key_surface_check;
ALTER TABLE virtual_key ADD CONSTRAINT virtual_key_surface_check CHECK (
    surface IN ('chat', 'ide', 'terminal')
    OR surface ~ '^agents/[a-z0-9][a-z0-9-]{0,38}$'
);
"""

GENESIS_HASH = "0" * 64


async def pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn=os.environ["CONTROL_PLANE_DATABASE_URL"],
            min_size=1,
            max_size=10,
        )
    return _pool


async def init() -> None:
    p = await pool()
    async with p.acquire() as conn:
        await conn.execute(SCHEMA)


def _digest(prev_hash: str, ts: str, actor: str, action: str, target: str | None, detail: dict) -> str:
    """Chain link. Field separator is NUL so no field value can forge a boundary."""
    payload = "\x00".join(
        [
            prev_hash,
            ts,
            actor,
            action,
            target or "",
            json.dumps(detail, sort_keys=True, separators=(",", ":")),
        ]
    )
    return hashlib.sha256(payload.encode()).hexdigest()


async def audit(actor: str, action: str, target: str | None = None, **detail) -> str:
    """Append one event to the chain and return its hash.

    Serialized against concurrent appenders: the chain is only meaningful if every
    writer reads the true tail, so the tail read and the insert share a transaction
    with a lock that blocks other appenders.
    """
    p = await pool()
    async with p.acquire() as conn:
        async with conn.transaction():
            # Advisory lock scoped to the audit chain. Cheaper than locking the table
            # and it does not block readers.
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext('audit_event'))")
            prev = await conn.fetchval(
                "SELECT hash FROM audit_event ORDER BY seq DESC LIMIT 1"
            )
            prev = prev or GENESIS_HASH
            row = await conn.fetchrow("SELECT now()::text AS ts")
            ts = row["ts"]
            h = _digest(prev, ts, actor, action, target, detail)
            await conn.execute(
                # $1 goes in as text and is cast in SQL. The chain hashes the *text*
                # rendering of the timestamp, so the value written and the value
                # verify_chain reads back with ts::text must be byte-identical — passing
                # a datetime here would let asyncpg choose its own rendering and break
                # verification.
                """
                INSERT INTO audit_event (ts, actor, action, target, detail, prev_hash, hash)
                VALUES ($1::text::timestamptz, $2, $3, $4, $5::jsonb, $6, $7)
                """,
                ts,
                actor,
                action,
                target,
                json.dumps(detail),
                prev,
                h,
            )
            return h


async def verify_chain() -> dict:
    """Recompute the whole chain. Returns the first break, if any.

    This is the test that makes the audit trail worth having — an append-only claim
    nobody checks is decoration.
    """
    p = await pool()
    async with p.acquire() as conn:
        rows = await conn.fetch(
            "SELECT seq, ts::text AS ts, actor, action, target, detail, prev_hash, hash "
            "FROM audit_event ORDER BY seq ASC"
        )
    expected_prev = GENESIS_HASH
    for r in rows:
        detail = r["detail"]
        if isinstance(detail, str):
            detail = json.loads(detail)
        if r["prev_hash"] != expected_prev:
            return {"ok": False, "broken_at": r["seq"], "reason": "prev_hash mismatch"}
        recomputed = _digest(
            r["prev_hash"], r["ts"], r["actor"], r["action"], r["target"], detail
        )
        if recomputed != r["hash"]:
            return {"ok": False, "broken_at": r["seq"], "reason": "hash mismatch"}
        expected_prev = r["hash"]
    return {"ok": True, "events": len(rows), "head": expected_prev}
