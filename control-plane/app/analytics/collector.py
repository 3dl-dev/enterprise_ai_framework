"""The ongoing ingestion collector — keeps the analytics records store fresh, in-cluster.

Runs as a background task in the control plane (registered in app/main.py, same shape as
agent_usage). Every tick it rebuilds the content-free records store from the canonical
sources it can reach:

  chat      LibreChat Mongo, in-process (pymongo) — always available
  ide       each workspace's opencode SQLite, fetched over the workspace shell-server's
            token-guarded /api/opencode-db route (there is no exec path into a workspace;
            the shell-server is the one authenticated in-cluster door — see the design record)
  cost      the gateway spend ledger, joined by <principal>::<surface> alias (metering)

MERGE, don't clobber: a tick replaces only the surfaces it successfully collected and keeps
the rest. So a workspace that is down (or a cluster where the opencode route is not deployed
yet) leaves the last good ide snapshot in place while chat keeps refreshing — the report
never goes blank because one source blinked.

This is the transcript-scrape path. Per decision -dee the forward canonical source is gateway
capture; when that lands it supersedes chat/ide collection here and these readers stay as the
enrichment for local-only coding detail. Until then, this is what makes the product ongoing.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile

import httpx

from . import ledger, librechat, opencode, report

# agent_usage (for its in-cluster k8s API helpers) and metering both bind asyncpg at import;
# they are imported lazily inside the functions that use them so the pure merge logic here
# needs no database driver to import or unit-test.

log = logging.getLogger("analytics.collector")

TENANT = os.environ.get("ANALYTICS_TENANT", "default")
COLLECT_SECONDS = float(os.environ.get("ANALYTICS_COLLECT_SECONDS", "1800"))
WS_TOKEN = os.environ.get("WORKSPACE_INTERNAL_TOKEN", "")
MONGO_URL = os.environ.get("CHAT_MONGO_URL", "")
MONGO_DB = os.environ.get("CHAT_MONGO_DB", "librechat")

WORKSPACE_SELECTOR = "app.kubernetes.io/component=workspace"
USER_LABEL = "workspace.enterprise-ai/user"


def enabled() -> bool:
    """Collect only where there is an in-cluster identity (the SA token) or an explicit
    override. False in the compose bundle and in unit tests, so importing is always safe."""
    override = os.environ.get("ANALYTICS_COLLECT_ENABLED")
    if override is not None:
        return override.strip().lower() in ("1", "true", "yes", "on")
    from .. import agent_usage
    return agent_usage.TOKEN_FILE.is_file()


# ------------------------------------------------------------------ chat (Mongo)


def collect_chat() -> list[dict]:
    """Normalize the whole LibreChat corpus. In-process pymongo, read-only by convention."""
    if not MONGO_URL:
        return []
    from pymongo import MongoClient  # lazy: no driver needed to import this module

    client = MongoClient(MONGO_URL, serverSelectionTimeoutMS=2000, connectTimeoutMS=2000)
    try:
        db = client[MONGO_DB]
        users = {str(u["_id"]): u.get("username")
                 for u in db.users.find({}, {"_id": 1, "username": 1})}
        messages = librechat.read_raw_messages(db)
        return librechat.normalize(
            messages, tenant=TENANT,
            resolve_principal=lambda uid: users.get(uid) or "(unattributed)",
        )
    finally:
        client.close()


# ------------------------------------------------------------------ opencode (ide)


async def _list_workspace_users(client: httpx.AsyncClient) -> list[str]:
    from .. import agent_usage
    resp = await client.get(
        f"{agent_usage.KUBE_API}/api/v1/namespaces/{agent_usage.namespace()}/pods",
        params={"labelSelector": WORKSPACE_SELECTOR},
        headers={"Authorization": f"Bearer {agent_usage._token()}"},
        timeout=20.0,
    )
    resp.raise_for_status()
    users = []
    for pod in resp.json().get("items", []):
        u = ((pod.get("metadata") or {}).get("labels") or {}).get(USER_LABEL)
        if u and u not in users:
            users.append(u)
    return users


async def _fetch_opencode(client: httpx.AsyncClient, user: str) -> list[dict]:
    """Fetch one workspace's opencode db over its shell-server and normalize it. Returns []
    (and logs) if the route is unavailable — an old ide snapshot is kept by the merge."""
    try:
        resp = await client.get(
            f"http://ws-{user}:7682/api/opencode-db",
            headers={"X-Workspace-Token": WS_TOKEN},
            timeout=120.0,
        )
        resp.raise_for_status()
        body = resp.content
    except Exception as exc:  # noqa: BLE001 - degrade, don't crash the tick
        log.warning("opencode fetch for %s failed: %s", user, exc)
        return []
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "opencode.db")
        with open(path, "wb") as f:
            f.write(body)
        raw = opencode.read_raw_sessions(path, immutable=True)  # a fresh, quiescent copy
        return opencode.normalize(raw, tenant=TENANT, principal=user, surface="ide")


async def collect_opencode() -> tuple[list[dict], set[str]]:
    """Normalize every reachable workspace's opencode db. Returns (records, users_seen) so
    the caller only overwrites the ide surface if at least one workspace answered."""
    from .. import agent_usage
    async with httpx.AsyncClient(verify=agent_usage._verify()) as client:
        users = await _list_workspace_users(client)
        got: list[dict] = []
        collected: set[str] = set()
        for user in users:
            recs = await _fetch_opencode(client, user)
            if recs:
                got.extend(recs)
                collected.add(user)
    return got, collected


# ------------------------------------------------------------------ merge + write


def merge_store(path: str, fresh: list[dict], surfaces: set[str]) -> int:
    """Replace records for the given surfaces with `fresh`, keep every other surface, write
    atomically. Returns the total record count written."""
    old_turns, old_sessions = report.load_records(path)
    kept = [r for r in old_turns + old_sessions if r.get("surface") not in surfaces]
    out = kept + fresh
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        for r in out:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")
    os.replace(tmp, path)  # atomic — a reader never sees a half-written store
    return len(out)


async def collect_once() -> dict:
    """One full tick: collect what is reachable, price it, merge into the store."""
    if not enabled():
        return {"enabled": False}

    records = list(collect_chat())
    surfaces = {"chat"} if records else set()

    ide_records, ide_users = await collect_opencode()
    if ide_users:
        records += ide_records
        surfaces.add("ide")

    turns = [r for r in records if r["k"] == "turn"]
    sessions = [r for r in records if r["k"] == "session"]
    if surfaces:
        try:
            sessions = await ledger.join(sessions, turns)
        except Exception as exc:  # noqa: BLE001 - unpriced beats no report
            log.warning("ledger join failed, recording without cost: %s", exc)

    total = merge_store(report.records_path(), turns + sessions, surfaces)
    return {
        "enabled": True,
        "surfaces": sorted(surfaces),
        "workspaces": sorted(ide_users),
        "turns": len(turns),
        "sessions": len(sessions),
        "store_records": total,
    }


# ------------------------------------------------------------------ the timer


async def _loop() -> None:
    while True:
        try:
            result = await collect_once()
            log.info("analytics collect: %s", result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("analytics collect failed: %s", exc)
        await asyncio.sleep(COLLECT_SECONDS)


def start_collector() -> asyncio.Task | None:
    if not enabled():
        log.info("analytics collector disabled (no in-cluster credential)")
        return None
    return asyncio.create_task(_loop(), name="analytics-collector")


async def stop_collector(task: asyncio.Task | None) -> None:
    if task is None:
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
