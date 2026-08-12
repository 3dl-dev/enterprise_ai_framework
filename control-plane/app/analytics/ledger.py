"""Join normalized sessions to the real billed ledger — the product's edge over the page.

The published coding-vs-orchestration page estimates cost from an API rate card. This
product has the actual bill: the gateway writes a spend row per request into
`LiteLLM_SpendLogs`, which `control-plane/app/metering.py` already reads for "the one bill".
This module stamps each session's records with REAL cost-per-edit / cost-per-turn, joined by
the `<principal>::<surface>` key alias over the session's time window.

Two layers, same split as the readers:

  attribute_costs(sessions, turns, spend_rows)   PURE: window each session from its turns,
                                                 bucket spend rows into it. Golden-testable.
  fetch_spend_rows(aliases, start, end)          the asyncpg query against the gateway db,
                                                 reusing metering's ONE attribution rule.
                                                 Exercised live (tests-live), like metering.

A session with no matching ledger rows gets `cost: {source: "none"}` — rendered "cost
unknown", NEVER a silent $0. A bare zero next to real edits is exactly the failure the
gateway's own unpriced-model detector (finding 4/31) exists to avoid.

Window-join caveat: opencode's session id is not propagated to the gateway, so attribution
is by alias + time window, not by a shared request id. Two genuinely concurrent sessions on
the same surface would have overlapping windows; a row is assigned to the most-recently-
started session active at its timestamp, which is correct for the common sequential case and
documented as approximate for the concurrent one.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

# gateway is pure string logic (no db driver); metering pulls asyncpg and is imported lazily
# inside the fetch path so the pure functions here need no database driver to run or test.
from ..gateway import surface_alias

# Spend rows are batch-flushed by LiteLLM (7-13s) plus a shutdown flush, and clocks between
# the surface and the gateway are not identical. Widen each window by this much on both ends
# so the request that opened or closed a turn is not missed at the boundary.
SKEW = timedelta(seconds=15)


def _parse(ts) -> datetime | None:
    """A turn ts (ISO string) or a spend startTime (datetime or ISO) -> aware UTC datetime."""
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    if isinstance(ts, str) and ts:
        try:
            d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def session_windows(turns: list[dict]) -> dict[str, dict]:
    """Per session: its alias and [first turn, last turn] window. Unknown surfaces skipped."""
    w: dict[str, dict] = {}
    for t in turns:
        ts = _parse(t.get("ts"))
        if ts is None:
            continue
        try:
            alias = surface_alias(t["principal"], t["surface"])
        except ValueError:
            continue  # a surface the gateway has no alias grammar for
        sid = t["sess"]
        e = w.get(sid)
        if e is None:
            w[sid] = {"alias": alias, "start": ts, "end": ts}
        else:
            e["start"] = min(e["start"], ts)
            e["end"] = max(e["end"], ts)
    return w


def attribute_costs(
    sessions: list[dict], turns: list[dict], spend_rows: list[dict]
) -> list[dict]:
    """Return `sessions` with a `cost` stamped on each from the ledger `spend_rows`.

    `spend_rows` are dicts with `alias`, `startTime`, `spend`, `total_tokens`. Pure — the
    caller fetches them (live) or a test supplies them.
    """
    windows = session_windows(turns)

    by_alias: dict[str, list] = defaultdict(list)
    for sid, win in windows.items():
        by_alias[win["alias"]].append((sid, win))
    for lst in by_alias.values():
        lst.sort(key=lambda x: x[1]["start"])  # earliest-started first

    acc = {sid: {"spend": 0.0, "tokens": 0, "n": 0} for sid in windows}
    for row in spend_rows:
        t = _parse(row.get("startTime"))
        if t is None:
            continue
        chosen = None
        for sid, win in by_alias.get(row.get("alias"), ()):  # sorted by start asc
            if win["start"] - SKEW <= t <= win["end"] + SKEW:
                chosen = sid  # keep the latest-started session that was active at t
        if chosen is not None:
            acc[chosen]["spend"] += float(row.get("spend") or 0)
            acc[chosen]["tokens"] += int(row.get("total_tokens") or 0)
            acc[chosen]["n"] += 1

    out = []
    for s in sessions:
        a = acc.get(s.get("sess"))
        if a and a["n"] > 0:
            cost = {
                "ledger_spend_usd": round(a["spend"], 6),
                "tokens": a["tokens"],
                "requests": a["n"],
                "source": "ledger",
            }
        else:
            # no ledger rows matched — say so, never imply $0 was really spent
            cost = {"source": "none"}
        out.append({**s, "cost": cost})
    return out


async def fetch_spend_rows(aliases: list[str], start: datetime, end: datetime) -> list[dict]:
    """Spend rows for the given aliases in [start, end]. Read-only, against the gateway db.

    Reuses `metering.ledger_attribution_sql` so the alias is derived by the SAME COALESCE
    (spend-row metadata first, deleted-key fallback second) as the one bill — a row this join
    can see is a row the bill can see. No live db in the hermetic suite; covered by tests-live.
    """
    if not aliases:
        return []
    from .. import metering  # lazy: keeps asyncpg off the pure-function import path

    # The shared-surface placeholder is only referenced by the principal/end-user fragments,
    # which we do not select — so it never appears in this query and we bind only $1..$3.
    attr = metering.ledger_attribution_sql("$99")
    sql = f"""
    SELECT {attr['alias']} AS alias,
           s."startTime"   AS "startTime",
           s.spend         AS spend,
           s.total_tokens  AS total_tokens
    {attr['join']}
    WHERE {attr['alias']} = ANY($1::text[])
      AND s."startTime" BETWEEN $2 AND $3
    """
    pool = await metering.pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, aliases, start - SKEW, end + SKEW)
    return [dict(r) for r in rows]


async def join(sessions: list[dict], turns: list[dict]) -> list[dict]:
    """Full ledger join: window the sessions, fetch their spend from the gateway, attribute."""
    windows = session_windows(turns)
    if not windows:
        return [{**s, "cost": {"source": "none"}} for s in sessions]
    aliases = sorted({w["alias"] for w in windows.values()})
    start = min(w["start"] for w in windows.values())
    end = max(w["end"] for w in windows.values())
    rows = await fetch_spend_rows(aliases, start, end)
    return attribute_costs(sessions, turns, rows)
