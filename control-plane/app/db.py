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

-- The LiteLLM -> freerouter CUTOVER MIRROR (enterpriseaiframework-1f8, phase 3 of the
-- cutover; design record docs/design/records/freerouter-reference-router.md C1).
--
-- Deliberately a SIDE table and not new columns on virtual_key. virtual_key holds the LIVE
-- LiteLLM key set — its gateway_token_hash is what /admin/budget and the spend join use — and
-- the whole point of the mirror is that it is ADDITIVE: every user keeps the LiteLLM key they
-- already hold, nobody re-logs in and nobody loses access, right up until GATEWAY_PROVIDER
-- flips. Writing the freerouter handle over gateway_token_hash would break budget updates and
-- spend attribution the moment the mirror ran, which is the opposite of a safe cutover.
--
-- key_alias is the join to virtual_key and the SAME "<user>::<surface>" string on both sides —
-- that identity is the whole mirror invariant, so it is the unique key here too.
--
-- limit_usd is what FREEROUTER REPORTED it applied (its per-key monthly cap is whole USD,
-- ceil()-rounded — internal/core/keys.go limitToMonthlyUSD), recorded from freerouter's own
-- response rather than echoed from our request, so the reconcile compares our intent against
-- the gateway's answer and not against itself. NULL means unlimited on both sides.
CREATE TABLE IF NOT EXISTS freerouter_mirror (
    id            BIGSERIAL PRIMARY KEY,
    key_alias     TEXT UNIQUE NOT NULL,
    -- freerouter's sub-account id: the durable, NON-SECRET handle the control plane revokes
    -- by. Never a bearer — the sub-account's one-time key is handed to the surface and the
    -- control plane keeps no copy, exactly as it keeps no copy of a LiteLLM virtual key.
    account_id    TEXT NOT NULL,
    -- SHA-256 of the minted user key, as freerouter reports it. Non-secret; it cannot
    -- authenticate. It is the id the key surface addresses, not a credential.
    key_hash      TEXT,
    -- The LiteLLM budget this row was mirrored FROM, snapshotted at mirror time.
    source_max_budget NUMERIC(12, 4),
    -- The cap freerouter said it applied, in whole USD. NULL = unlimited.
    limit_usd     INTEGER,
    mirrored_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The SECOND metering dimension (enterpriseaiframework-914, Contract 3 of
-- docs/design/records/agents-surface.md): how long each agent was RESIDENT and what it
-- burned. Deliberately its own table and NOT the inference ledger — inference spend lives
-- in the gateway's own database and is read, never written, by this service. Composing
-- the two happens in the endpoint layer, so `metering.spend_by_user_and_surface` and
-- therefore `/admin/spend` are byte-unchanged by this table existing.
--
-- QUANTITIES ONLY. Baron's ruling: meter usage, not cost. Owned hardware is sunk cost and
-- there is no real dollar figure to record here, so there is no rate column, no cost
-- column and no currency anywhere in this table. See app/agent_usage.py.
--
-- The columns split into two groups and the split is load-bearing:
--   * `resident_seconds` / `cpu_core_seconds` / `memory_peak_bytes` are DURABLE totals.
--     They only ever go up. Nothing recomputes them from scratch, so a collector restart
--     or a pod replacement cannot make them fall.
--   * `last_*` is the bookmark the next sample deltas against. `last_pod_uid` in
--     particular is what makes a new pod incarnation start its CPU delta at zero
--     (cAdvisor's counter restarts with the container) instead of subtracting the old
--     pod's counter from the new one's.
CREATE TABLE IF NOT EXISTS agent_usage (
    id                BIGSERIAL PRIMARY KEY,
    -- The attribution key, taken from the pod labels agent.enterprise-ai/user and
    -- agent.enterprise-ai/name — not from the virtual key. Compute is consumed by the
    -- POD, so a BYO agent with no gateway ledger row at all still has usage here.
    agent_user        TEXT NOT NULL,
    agent_name        TEXT NOT NULL,
    resident_seconds  DOUBLE PRECISION NOT NULL DEFAULT 0,
    cpu_core_seconds  DOUBLE PRECISION NOT NULL DEFAULT 0,
    memory_peak_bytes BIGINT NOT NULL DEFAULT 0,
    -- Where the compute number came from, or NULL if it was never measurable here. NULL
    -- next to a zero means "not measured"; a source next to a zero means "measured, and
    -- it really was idle". A bare zero cannot say which, and an unmetered path that
    -- renders as healthy is finding 4.
    compute_source    TEXT,
    model_source      TEXT,
    last_pod_uid      TEXT,
    last_pod_name     TEXT,
    last_pod_phase    TEXT,
    last_cpu_counter  DOUBLE PRECISION,
    last_observed_at  TIMESTAMPTZ,
    first_seen_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (agent_user, agent_name)
);

-- Same reasoning as the virtual_key CHECK above, in the form Postgres does offer for
-- columns: CREATE TABLE IF NOT EXISTS is a no-op against a database that already has an
-- earlier shape of this table, so every column is also asserted individually. Idempotent
-- on every boot, and the failure it prevents is an UndefinedColumnError at the moment an
-- operator first opens the usage view — which reads as "the meter is broken".
ALTER TABLE agent_usage ADD COLUMN IF NOT EXISTS resident_seconds  DOUBLE PRECISION NOT NULL DEFAULT 0;
ALTER TABLE agent_usage ADD COLUMN IF NOT EXISTS cpu_core_seconds  DOUBLE PRECISION NOT NULL DEFAULT 0;
ALTER TABLE agent_usage ADD COLUMN IF NOT EXISTS memory_peak_bytes BIGINT NOT NULL DEFAULT 0;
ALTER TABLE agent_usage ADD COLUMN IF NOT EXISTS compute_source    TEXT;
ALTER TABLE agent_usage ADD COLUMN IF NOT EXISTS model_source      TEXT;
ALTER TABLE agent_usage ADD COLUMN IF NOT EXISTS last_pod_uid      TEXT;
ALTER TABLE agent_usage ADD COLUMN IF NOT EXISTS last_pod_name     TEXT;
ALTER TABLE agent_usage ADD COLUMN IF NOT EXISTS last_pod_phase    TEXT;
ALTER TABLE agent_usage ADD COLUMN IF NOT EXISTS last_cpu_counter  DOUBLE PRECISION;
ALTER TABLE agent_usage ADD COLUMN IF NOT EXISTS last_observed_at  TIMESTAMPTZ;
ALTER TABLE agent_usage ADD COLUMN IF NOT EXISTS first_seen_at     TIMESTAMPTZ NOT NULL DEFAULT now();
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
