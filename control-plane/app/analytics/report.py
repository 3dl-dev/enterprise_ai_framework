"""Assemble the report the operator page renders — read the records store, slice, return.

The heavy work (read every surface, normalize, join the ledger) is done ahead of time by the
ingestion collector, which writes CONTENT-FREE normalized+priced records to a durable store.
This module reads that store and slices it — no database, no live-pod access at request time,
so the operator page is fast and the request path is trivially safe.

  load_records(path)   the store (JSONL of turn/session records) -> (turns, sessions)
  assemble(...)        window-filter + slice -> the metrics.json the page consumes

The store is written by a separate ingestion item; until it exists the store is simply
absent, and assemble returns a valid empty report so the page shows "no data yet" rather
than erroring.
"""

from __future__ import annotations

import json
import os

from . import metrics, slicing

# Where the ingestion collector writes normalized+priced records. A control-plane volume in
# the cluster; a path in the compose bundle. Absent is fine — it means nothing ingested yet.
RECORDS_PATH_ENV = "ANALYTICS_RECORDS_PATH"


def records_path() -> str:
    return os.environ.get(RECORDS_PATH_ENV, "/var/lib/enterprise-ai/analytics/records.jsonl")


def load_records(path: str | None = None) -> tuple[list[dict], list[dict]]:
    """Read the records store. Missing/empty -> ([],[]); a malformed line is skipped, not
    fatal — one bad row must not blank the whole report."""
    path = path or records_path()
    turns: list[dict] = []
    sessions: list[dict] = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                (turns if r.get("k") == "turn" else sessions).append(r)
    except OSError:
        return [], []
    return turns, sessions


def _in_window(ts: str | None, since: str | None, until: str | None) -> bool:
    day = (ts or "")[:10]
    if since and day < since:
        return False
    if until and day > until:
        return False
    return True


def assemble(
    turns: list[dict],
    sessions: list[dict],
    *,
    dimension: str = "model",
    tenant: str | None = None,
    since: str | None = None,
    until: str | None = None,
    min_n: int = metrics.MIN_N,
) -> dict:
    """Window-filter the corpus, then slice it by `dimension`. Returns the metrics.json
    structure the page renders, with the offered dimensions attached for the selector."""
    if since or until:
        turns = [t for t in turns if _in_window(t.get("ts"), since, until)]
        kept = {t["sess"] for t in turns}
        sessions = [s for s in sessions if s.get("sess") in kept]

    m = slicing.slice_metrics(turns, sessions, dimension, tenant=tenant, min_n=min_n)
    m["dimensions"] = slicing.available_dimensions(turns)
    m["meta"] = {**m.get("meta", {}), "since": since, "until": until}
    return m


def report(
    *, dimension: str = "model", tenant: str | None = None,
    since: str | None = None, until: str | None = None,
    min_n: int = metrics.MIN_N, path: str | None = None,
) -> dict:
    """Convenience: load the store and assemble in one call. What the endpoint invokes."""
    turns, sessions = load_records(path)
    return assemble(turns, sessions, dimension=dimension, tenant=tenant,
                    since=since, until=until, min_n=min_n)
