"""The LiteLLM -> freerouter cutover mirror, against a real freerouter and a real Postgres.

enterpriseaiframework-1f8. Nothing here is staged: the freerouter under test is the actual
`cmd/freerouter` binary serving on loopback over its own SQLite meter, and the gateway
database is a real, disposable postgres:16 seeded with real `principal` / `virtual_key` rows
through the SAME `app.db.SCHEMA` the service boots. Both are per-module and randomly ported,
so this never touches the shared bundle or another agent's stack.

That matters more than usual for this item, because the two facts the design turns on are
both facts about freerouter's real behaviour and neither survives a fake:

  1. `GET /api/v1/usage/rollup` is derived from recorded GENERATION events, so a sub-account
     that has never spent is absent from it. A hand-written double would have listed every
     account it had created and every one of these tests would have passed while the
     production reconcile called the whole mirror "missing".
  2. A per-key cap can only be set BY THE SUB-ACCOUNT at mint (`POST /api/v1/keys`), is whole
     USD, and freerouter rounds it UP. The mirror's whole budget story rests on freerouter's
     own reported `limit`, so the value has to come from freerouter.

The one thing that is stubbed is LiteLLM, and only in the one test that asks LiteLLM itself
(`--verify-litellm`): a LiteLLM server is not what this item builds, the alias listing it
provides is a one-field read, and every other test derives the LiteLLM side from the real
gateway database instead — which is what the item's done-condition names.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _real_asyncpg():
    """asyncpg itself, even when a sibling module has parked a STUB under that name.

    Six control-plane test modules do `if "asyncpg" not in sys.modules: sys.modules["asyncpg"]
    = types.ModuleType("asyncpg")`, because they only need app.db to import and the venv used
    to carry no driver. Several of them collect before this file, so a plain `import asyncpg`
    here hands back their stub — whose `create_pool` raises "no database in this suite" — and
    every connection below silently fails against a Postgres that is up and healthy. That is
    exactly how this file passed alone and errored with "postgres never became reachable" in a
    full run.

    So: drop the parked stub, import the genuine package, then put the stub back byte for
    byte. Nothing else in the suite sees a different asyncpg than it saw before.
    """
    def _import():
        try:
            import asyncpg

            return asyncpg
        except ImportError:
            return None

    parked = sys.modules.get("asyncpg")
    if parked is None or getattr(parked, "__file__", None):
        # Nothing parked, or the real package already imported: the ordinary path.
        return _import()

    # A stub is in the way. Take it out, import for real, then put it back exactly as it was
    # — the real package keeps its own references to its submodules, so it goes on working.
    del sys.modules["asyncpg"]
    try:
        real = _import()
    finally:
        for name in [n for n in list(sys.modules) if n == "asyncpg" or n.startswith("asyncpg.")]:
            del sys.modules[name]
        sys.modules["asyncpg"] = parked
    return real


asyncpg = _real_asyncpg()
if asyncpg is None:
    pytest.skip("asyncpg is not installed; the mirror is a Postgres feature",
                allow_module_level=True)

from app import chat_identity, db, freerouter, gateway, issuance, mirror, provisioning  # noqa: E402

FR_BINARY = Path(os.environ.get("FREEROUTER_BINARY", "/tmp/fr-enterpriseaiframework-1f8"))
FR_SOURCE = Path(os.environ.get("FREEROUTER_SOURCE", "/home/baron/projects/freerouter"))


# The running freerouter's own SQLite meter file, recorded by the server fixture. Written to
# by `_record_spend` below — see there for why a test needs to reach it.
METER: dict = {}


def _record_spend(account_id: str, cost_usd: float = 0.25) -> None:
    """Put a real generation event in freerouter's meter so the account appears in the rollup.

    freerouter's `GET /api/v1/usage/rollup` is built from `usage_events` and NOTHING else
    (metering/rollup.go aggregates them, then filters to the caller's subtree), and its SQLite
    store re-reads the table on every call rather than caching — so a row inserted here is
    read back by the RUNNING binary through its own aggregation and subtree walk. That is the
    only way to reach the rollup at all in this suite: producing spend the ordinary way needs
    a real upstream provider, and there is none here.

    This is the meter's real table, in the real schema, read by the real service. What is
    staged is the completion that would have written the row, not the read under test.
    """
    import sqlite3

    event = {
        "account_id": account_id,
        "key_hash_prefix": "00000000",
        "model_id": "test/model",
        "provider": "test",
        "status": "ok",
        "input_tokens": 100,
        "output_tokens": 50,
        "cost_usd": cost_usd,
        "generation_id": f"gen-{account_id}-{uuid.uuid4().hex[:8]}",
        "timestamp": "2026-09-01T00:00:00Z",
    }
    conn = sqlite3.connect(str(METER["db"]))
    try:
        conn.execute(
            "INSERT INTO usage_events (generation_id, account_id, timestamp, event_json) "
            "VALUES (?, ?, ?, ?)",
            (event["generation_id"], account_id, event["timestamp"], json.dumps(event)),
        )
        conn.commit()
    finally:
        conn.close()


def _clear_spend() -> None:
    """Empty the meter's usage table so each test starts with the rollup it expects.

    The freerouter process is module-scoped while the gateway database is per-test, so an
    event left behind by one test is an account id from a DEAD world still wearing a live
    alias in the next test's rollup — which reads, correctly but uselessly, as an
    account_id_conflict. Cleared at the top of `world`, alongside the schema reset.
    """
    import sqlite3

    if not METER.get("db") or not METER["db"].exists():
        return
    conn = sqlite3.connect(str(METER["db"]))
    try:
        conn.execute("DELETE FROM usage_events")
        conn.commit()
    finally:
        conn.close()


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ---------------------------------------------------------------- a real freerouter


@pytest.fixture(scope="module")
def freerouter_server(tmp_path_factory):
    """The real `cmd/freerouter` binary, on a random port, over its own SQLite meter."""
    binary = FR_BINARY
    if not binary.exists():
        if not (FR_SOURCE / "go.mod").exists():
            pytest.skip(f"no freerouter binary at {binary} and no source at {FR_SOURCE}")
        build = subprocess.run(
            ["go", "build", "-o", str(binary), "./cmd/freerouter"],
            cwd=FR_SOURCE, capture_output=True, text=True, timeout=600,
        )
        assert build.returncode == 0, f"go build failed: {build.stderr}"

    workdir = tmp_path_factory.mktemp("freerouter")
    port = free_port()
    METER["db"] = workdir / "meter.db"
    env = {
        **os.environ,
        "FREEROUTER_METER_BACKEND": "sqlite",
        "FREEROUTER_METER_DSN": str(workdir / "meter.db"),
        "FREEROUTER_LISTEN_ADDR": f"127.0.0.1:{port}",
        "FREEROUTER_SIGNUP": "open",
    }
    log = (workdir / "freerouter.log").open("w")
    proc = subprocess.Popen([str(binary)], env=env, stdout=log, stderr=subprocess.STDOUT)

    import httpx

    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30
    ready = False
    while time.monotonic() < deadline and proc.poll() is None:
        try:
            if httpx.get(f"{base}/healthz", timeout=1.0).status_code == 200:
                ready = True
                break
        except Exception:
            time.sleep(0.2)
    if not ready:
        proc.kill()
        log.close()
        pytest.fail(f"freerouter never came up:\n{(workdir / 'freerouter.log').read_text()}")

    yield base

    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
    log.close()


@pytest.fixture(scope="module")
def control_plane_tenant(freerouter_server):
    """The control plane's own freerouter tenant, signed up for real."""
    import httpx

    resp = httpx.post(
        f"{freerouter_server}/api/v1/signup",
        json={"display_name": "enterprise-ai-control-plane"},
        timeout=10.0,
    )
    resp.raise_for_status()
    return resp.json()["data"]["api_key"]


# ---------------------------------------------------------------- a real gateway database


@pytest.fixture(scope="module")
def postgres():
    """A disposable postgres:16, never the shared bundle's."""
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        pytest.skip("docker unavailable; the mirror needs a real Postgres")
    name = f"eaf-test-mirror-pg-{uuid.uuid4().hex[:8]}"
    port = free_port()
    run = subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", name,
         "-p", f"127.0.0.1:{port}:5432",
         "-e", "POSTGRES_PASSWORD=mirror", "-e", "POSTGRES_DB=controlplane",
         "postgres:16-alpine"],
        capture_output=True, text=True, timeout=120,
    )
    assert run.returncode == 0, f"docker run postgres:16-alpine failed: {run.stderr}"

    dsn = f"postgresql://postgres:mirror@127.0.0.1:{port}/controlplane"

    async def wait():
        last = None
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                conn = await asyncpg.connect(dsn)
                await conn.close()
                return None
            except Exception as exc:  # noqa: BLE001 - reported verbatim on give-up
                last = exc
                await asyncio.sleep(0.4)
        return last or RuntimeError("deadline with no attempt")

    failure = asyncio.run(wait())
    if failure is not None:
        logs = subprocess.run(["docker", "logs", name], capture_output=True, text=True)
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        pytest.fail(
            f"postgres never became reachable at {dsn}: "
            f"{type(failure).__name__}: {failure}\n{logs.stdout}\n{logs.stderr}"
        )

    yield dsn

    subprocess.run(["docker", "rm", "-f", name], capture_output=True)


# ---------------------------------------------------------------- the world under test


# Two users, five live surfaces, three distinct budget shapes, and one REVOKED key that must
# NOT be mirrored. The budgets are chosen to separate the two budget buckets the reconcile
# reports: 25.0 mirrors exactly, 0.5 can only mirror as 1 (freerouter's cap is whole USD),
# and NULL is unlimited on both sides.
SEED = [
    ("alice", "chat", 25.0, "active"),
    ("alice", "ide", 0.5, "active"),
    ("alice", "terminal", None, "active"),
    ("bob", "chat", 10.0, "active"),
    ("bob", "agents/scraper", 3.25, "active"),
    ("carol", "chat", 5.0, "revoked"),
]


# One event loop per test, and every coroutine in the test runs on it. asyncio.run() would
# create and destroy a loop per call, and an asyncpg pool is bound to the loop that made it —
# so a pool opened in one run() and used in the next blows up with "Event loop is closed",
# which says nothing about the mirror.
_LOOP: dict = {"loop": None}


def run(coro):
    return _LOOP["loop"].run_until_complete(coro)


@pytest.fixture()
def world(postgres, freerouter_server, control_plane_tenant, monkeypatch):
    """A freshly-schema'd gateway database seeded with SEED, wired to the real freerouter."""
    monkeypatch.setenv("CONTROL_PLANE_DATABASE_URL", postgres)
    monkeypatch.setenv("FREEROUTER_URL", freerouter_server)
    monkeypatch.setenv("FREEROUTER_MASTER_KEY", control_plane_tenant)
    monkeypatch.setenv("GATEWAY_PROVIDER", "freerouter")
    # app.db caches its pool in a module global; a per-test pool against a per-test schema is
    # what keeps these independent of order.
    monkeypatch.setattr(db, "_pool", None, raising=False)
    # The gateway database is per-test but the freerouter process is per-module, so its meter
    # is reset here too — otherwise one test's recorded spend is a stale account id wearing a
    # live alias in the next test's rollup.
    _clear_spend()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _LOOP["loop"] = loop

    async def setup():
        conn = await asyncpg.connect(postgres)
        await conn.execute(
            "DROP TABLE IF EXISTS freerouter_mirror, agent_usage, audit_event, "
            "virtual_key, principal CASCADE"
        )
        await conn.execute(db.SCHEMA)
        for username, surface, budget, status in SEED:
            pid = await conn.fetchval(
                "INSERT INTO principal (idp_user_id, username, email, enabled) "
                "VALUES ($1, $2, $3, TRUE) "
                "ON CONFLICT (idp_user_id) DO UPDATE SET username = EXCLUDED.username "
                "RETURNING id",
                f"idp-{username}", username, f"{username}@example.test",
            )
            await conn.execute(
                "INSERT INTO virtual_key (principal_id, surface, key_alias, "
                "gateway_token_hash, max_budget, status) VALUES ($1, $2, $3, $4, $5, $6)",
                pid, surface, gateway.surface_alias(username, surface),
                f"litellm-hash-{username}-{surface}", budget, status,
            )
        await conn.close()

    loop.run_until_complete(setup())

    pool_holder = {}

    async def _pool():
        if "pool" not in pool_holder:
            pool_holder["pool"] = await asyncpg.create_pool(postgres, min_size=1, max_size=4)
        return pool_holder["pool"]

    monkeypatch.setattr(db, "pool", _pool)
    # The same call main.lifespan makes, so what is under test is the wiring the service
    # actually performs and not a hand-assigned attribute.
    monkeypatch.setattr(freerouter, "alias_resolver", None)
    mirror.install_alias_resolver()

    yield {"dsn": postgres, "base": freerouter_server}

    async def teardown():
        if "pool" in pool_holder:
            await pool_holder["pool"].close()

    loop.run_until_complete(teardown())
    loop.close()
    _LOOP["loop"] = None


# ---------------------------------------------------------------- the mirror


def test_the_selector_is_what_provisions(world, monkeypatch):
    """Mirroring while the selector still points at LiteLLM must refuse, not go around it.

    The constraint is that provisioning happens through provisioning.backend(); a mirror that
    imported app.freerouter directly would work here and be a second mint path in production.
    """
    monkeypatch.setenv("GATEWAY_PROVIDER", "litellm")
    assert provisioning.backend() is gateway
    with pytest.raises(mirror.BackendNotFreerouter):
        run(mirror.mirror())


def test_mirror_creates_one_subaccount_per_live_key_with_the_same_alias(world):
    result = run(mirror.mirror())

    # Five live keys; carol's revoked key is not one of them. Mirroring a revoked key would
    # hand a disabled principal a working freerouter key.
    assert result["source_keys"] == 5
    assert result["minted"] == 5
    minted = {d["key_alias"] for d in result["details"]}
    assert minted == {
        "alice::chat", "alice::ide", "alice::terminal",
        "bob::chat", "bob::agents/scraper",
    }
    assert "carol::chat" not in minted

    # The alias identity is the mirror invariant: the SAME string on both sides.
    rows = run(_mirror_rows(world["dsn"]))
    assert set(rows) == minted
    assert all(r["account_id"] for r in rows.values())


def test_mirrored_budget_is_the_cap_freerouter_reports_it_applied(world):
    """The budget check reads FREEROUTER's answer, not our request echoed back."""
    run(mirror.mirror())
    rows = run(_mirror_rows(world["dsn"]))

    # 25.0 -> 25 exactly; None -> unlimited (null).
    assert rows["alice::chat"]["limit_usd"] == 25
    assert rows["alice::terminal"]["limit_usd"] is None
    assert rows["bob::chat"]["limit_usd"] == 10
    # freerouter's cap is WHOLE USD and rounds UP, so a sub-dollar budget can only mirror as
    # $1. Looser than LiteLLM's, never tighter — nobody loses spend they had.
    assert rows["alice::ide"]["limit_usd"] == 1
    assert rows["bob::agents/scraper"]["limit_usd"] == 4


def test_the_mirrored_cap_is_the_one_freerouter_serves_back(world, freerouter_server):
    """Read the cap back OUT of freerouter, with the key the mirror actually minted.

    The recorded limit_usd could in principle be anything we chose to write down. This proves
    the number is freerouter's: it authenticates as the sub-account and asks freerouter what
    cap that key carries.
    """
    import httpx

    # Mint one directly so the test holds the bearer the mirror hands to a surface.
    created = run(provisioning.generate_key(
        username="alice", surface="chat", idp_user_id="idp-alice", max_budget=25.0,
    ))
    served = httpx.get(
        f"{freerouter_server}/api/v1/keys",
        headers={"Authorization": f"Bearer {created['key']}"}, timeout=10.0,
    )
    served.raise_for_status()
    ours = [k for k in served.json()["data"] if k["hash"] == created["key_hash"]]
    assert len(ours) == 1, served.json()
    assert ours[0]["name"] == "alice::chat"
    assert ours[0]["limit"] == 25
    assert created["limit_usd"] == 25


def test_mirror_is_idempotent(world):
    first = run(mirror.mirror())
    second = run(mirror.mirror())
    assert first["minted"] == 5
    assert second["minted"] == 0
    assert second["already_mirrored"] == 5
    # Two accounts wearing one alias would make the operator bill ambiguous.
    rows = run(_mirror_rows(world["dsn"]))
    assert len(rows) == 5
    assert len({r["account_id"] for r in rows.values()}) == 5


def test_mirror_never_touches_the_live_litellm_key_set(world):
    """No user re-logs in and no user loses access: virtual_key comes out byte-identical."""
    before = run(_virtual_keys(world["dsn"]))
    run(mirror.mirror())
    after = run(_virtual_keys(world["dsn"]))
    assert before == after


# --------------------------------- the chat surface's shared key (enterpriseaiframework-e6b)
#
# LibreChat holds ONE key for every user (chat_identity.py): "chat-surface::chat". Both
# operator scripts that have ever minted it (bundle/bin/provision-chat-key.sh,
# deploy/bin/post-deploy.sh) talk straight to the gateway, bypassing issuance.issue —
# because "chat-surface" is not a real IdP identity and issuance._resolve_principal used to
# 404 on it. That left the key with no virtual_key row, which is exactly the row
# SOURCE_SQL above reads: the shared key was invisible to the mirror, so the cutover would
# have flipped GATEWAY_PROVIDER while LibreChat's stored credential still pointed at a
# LiteLLM key nobody had mirrored to freerouter. These tests prove the fix end to end
# against the real freerouter binary and postgres `world` already wires: issuance.issue can
# now mint/rotate the shared key, the mint lands through provisioning.backend() (so it
# already targets freerouter under GATEWAY_PROVIDER=freerouter, exactly as the flip needs),
# and the mirror — unmodified — now includes it.


def test_issuing_the_shared_chat_key_never_reaches_the_idp(world, monkeypatch):
    """chat-surface is not a person; resolving its principal must not call identity.get_user."""

    async def explode(username):
        raise AssertionError(f"identity.get_user reached for the shared surface: {username}")

    monkeypatch.setattr(issuance.identity, "get_user", explode)

    result = run(issuance.issue(
        chat_identity.SHARED_SURFACE_PRINCIPAL, "chat", actor="operator",
    ))

    assert result["key_alias"] == "chat-surface::chat"
    assert result["key"]  # a real freerouter bearer, minted through provisioning.backend()


def test_the_shared_chat_key_is_now_a_real_virtual_key_row(world):
    run(issuance.issue(chat_identity.SHARED_SURFACE_PRINCIPAL, "chat", actor="operator"))

    rows = run(_virtual_keys(world["dsn"]))
    aliases = {r[0]: r for r in rows}
    assert "chat-surface::chat" in aliases, (
        "the shared key still has no virtual_key row — SOURCE_SQL, and therefore the "
        "mirror, cannot see it"
    )
    assert aliases["chat-surface::chat"][3] == "active"


def test_the_mirror_now_carries_the_shared_chat_key_across_the_flip(world):
    """The actual done-condition: mirror() — unmodified — picks the shared key up."""
    run(issuance.issue(chat_identity.SHARED_SURFACE_PRINCIPAL, "chat", actor="operator"))

    result = run(mirror.mirror())

    minted = {d["key_alias"] for d in result["details"]}
    assert "chat-surface::chat" in minted
    mirrored = run(_mirror_rows(world["dsn"]))
    assert "chat-surface::chat" in mirrored
    report = run(mirror.reconcile())
    assert report["missing"] == []


def test_rotating_the_shared_chat_key_remaps_it_in_place_without_touching_the_idp(
    world, monkeypatch
):
    """The item's DONE condition: re-issuing hands back a NEW, live credential — the remap
    LibreChat's held key rides across the flip on — while nothing about IdP identity moves,
    because no end user is involved in this key at all."""

    async def explode(username):
        raise AssertionError("a rotate of the shared key must not touch the IdP")

    monkeypatch.setattr(issuance.identity, "get_user", explode)

    first = run(issuance.issue(chat_identity.SHARED_SURFACE_PRINCIPAL, "chat", actor="operator"))
    assert first["rotated"] is False  # first mint in this world

    second = run(issuance.issue(chat_identity.SHARED_SURFACE_PRINCIPAL, "chat", actor="operator"))
    assert second["rotated"] is True
    assert second["key"] != first["key"], "rotate must hand back a genuinely new credential"

    rows = run(_virtual_keys(world["dsn"]))
    aliases = {r[0]: r for r in rows}
    assert aliases["chat-surface::chat"][3] == "active"

    # The outcome that matters to LibreChat: can it still authenticate? Not "was an endpoint
    # called" — checked against the real freerouter binary, both ways (Q2). The OLD bearer
    # must stop working (issue() deletes the prior alias before minting the new one) and the
    # NEW bearer, the one the operator would push into LibreChat's config, must work.
    import httpx

    def authenticates(key: str) -> bool:
        r = httpx.get(
            f"{world['base']}/api/v1/keys",
            headers={"Authorization": f"Bearer {key}"}, timeout=10.0,
        )
        return r.status_code == 200

    assert not authenticates(first["key"]), (
        "the pre-rotation credential still authenticates — LibreChat's OLD key would "
        "silently keep working instead of being remapped"
    )
    assert authenticates(second["key"]), "the remapped credential must actually work"


# ---------------------------------------------------------------- the reconcile


def test_reconcile_reports_zero_missing_and_zero_budget_mismatch_after_a_mirror(world):
    """The item's gate, against the real gateway DB and the real freerouter."""
    run(mirror.mirror())
    report = run(mirror.reconcile())

    assert report["missing"] == []
    assert report["budget_mismatch"] == []
    assert report["orphans"] == []
    assert report["account_id_conflict"] == []
    assert report["source_keys"] == 5
    assert report["mirrored_keys"] == 5
    assert report["ok"] is True

    # And the rounding is REPORTED rather than absorbed into that green.
    rounded = {r["key_alias"] for r in report["budget_rounded"]}
    assert rounded == {"alice::ide", "bob::agents/scraper"}


def test_reconcile_reports_a_never_mirrored_key_as_missing(world):
    """Break it the way it actually breaks: a key that exists in the gateway DB and nowhere
    else. The fault is injected through the DATA the reconcile reads, not by editing it."""
    run(mirror.mirror())
    run(_delete_mirror_row(world["dsn"], "bob::chat"))

    report = run(mirror.reconcile())
    assert report["missing"] == ["bob::chat"]
    assert report["ok"] is False


def test_reconcile_catches_a_budget_that_did_not_carry_across(world):
    """A cap that disagrees with the LiteLLM budget must not read as green.

    Injected by raising the LiteLLM budget in the gateway database AFTER the mirror ran —
    exactly the drift an operator creates with /admin/budget between the mirror and the flip,
    and exactly what freerouter cannot follow (its cap is fixed at mint).
    """
    run(mirror.mirror())
    clean = run(mirror.reconcile())
    assert clean["budget_mismatch"] == []  # control: green before the fault

    run(_set_budget(world["dsn"], "alice::chat", 99.0))
    report = run(mirror.reconcile())

    assert [m["key_alias"] for m in report["budget_mismatch"]] == ["alice::chat"]
    assert report["budget_mismatch"][0]["expected_limit_usd"] == 99
    assert report["budget_mismatch"][0]["freerouter_limit_usd"] == 25
    assert report["ok"] is False


def test_reconcile_reports_an_orphan_when_the_litellm_key_goes_away(world):
    """A sub-account outliving its LiteLLM key would keep working after the flip."""
    run(mirror.mirror())
    run(_revoke_virtual_key(world["dsn"], "alice::terminal"))

    report = run(mirror.reconcile())
    assert report["orphans"] == ["alice::terminal"]
    assert report["missing"] == []


def test_unspent_subaccounts_are_reported_unconfirmed_not_verified(world):
    """freerouter's rollup is built from spend events, so it cannot corroborate an unused
    sub-account. The reconcile must say so rather than claim a verification it did not do."""
    run(mirror.mirror())
    report = run(mirror.reconcile())

    # Nothing in this world has spent anything, so freerouter confirms nothing...
    assert report["confirmed"] == []
    assert len(report["unconfirmed"]) == 5
    # ...and that is NOT laundered into "missing", which would have blocked a correct cutover.
    assert report["missing"] == []
    assert report["ok"] is True

    # The claim above is about freerouter, so read it from freerouter.
    assert run(freerouter.rollup_account_ids_by_alias()) == {}


def test_reconcile_can_ask_litellm_itself(world, monkeypatch):
    """--verify-litellm catches a key minted straight at the gateway, behind our back."""
    run(mirror.mirror())

    async def live_aliases(prefix=None):
        return [
            "alice::chat", "alice::ide", "alice::terminal",
            "bob::chat", "bob::agents/scraper",
            "mallory::chat",  # minted at LiteLLM directly; the gateway DB has never seen it
        ]

    monkeypatch.setattr(gateway, "list_aliases", live_aliases)
    report = run(mirror.reconcile(verify_litellm=True))
    assert report["litellm_only"] == ["mallory::chat"]
    assert report["ok"] is False


# ------------------------------------------- the alias resolver, and the revoke it unblocks


def test_revoking_an_unspent_subaccount_works_only_because_of_our_own_record(world):
    """The defect the resolver exists for, run BOTH ways on the real freerouter.

    The outcome asserted is the one that matters to a user: can the key still authenticate?
    Not "was an endpoint called" and not "does an account row still exist" — freerouter's
    revoke deliberately leaves the row (and the bill) in place and only kills the keys, so a
    second DELETE still answers 200 and proves nothing.

    Defect present (resolver unset, the read path exactly as it stood before this item): the
    alias resolves from the spend-derived rollup, an unspent sub-account is not in it, and
    `delete_by_aliases(..., missing_ok=True)` — the ROTATE path in issuance.issue and the
    disabled-in-IdP revoke in main.sync — returns success while the key keeps working.
    Fixed: the same call, same freerouter, and the key stops authenticating.
    """
    import httpx

    alias = "dave::chat"
    created = run(provisioning.generate_key(
        username="dave", surface="chat", idp_user_id="idp-dave", max_budget=None,
    ))
    run(_insert_mirror_row(world["dsn"], alias, created["token"], created.get("key_hash")))

    def key_authenticates() -> bool:
        r = httpx.get(
            f"{world['base']}/api/v1/keys",
            headers={"Authorization": f"Bearer {created['key']}"}, timeout=10.0,
        )
        return r.status_code == 200

    assert key_authenticates(), "the freshly minted key should work before any revoke"

    # --- defect present
    freerouter.alias_resolver = None
    try:
        assert run(freerouter.list_aliases(prefix="dave::")) == []
        assert run(freerouter.delete_by_aliases([alias], missing_ok=True)) == {
            "deleted_keys": []
        }
        with pytest.raises(KeyError):
            run(freerouter.delete_by_aliases([alias]))
        assert key_authenticates(), (
            "THE DEFECT: revoke reported success and the user's key still works"
        )
    finally:
        mirror.install_alias_resolver()

    # --- fixed
    assert alias in run(freerouter.list_aliases(prefix="dave::"))
    assert run(freerouter.delete_by_aliases([alias]))["deleted_keys"] == [alias]
    assert not key_authenticates(), "after revoke the key must no longer authenticate"


# ------------------------------------------------------ /admin/budget on the two backends
#
# enterpriseaiframework-257, the ADMITTED flip blocker: `main.set_budget` →
# `provisioning.update_budget` → `freerouter.update_budget` used to raise NotImplementedError,
# so POST /admin/budget was a 500 on the freerouter backend and the flip could not happen with
# budgets working. These drive the real endpoint function against the real freerouter binary
# and the real gateway database, and the assertions are about what FREEROUTER says the cap is
# and whether a key can still authenticate — never about which functions were called.


def _key_state(base: str, key: str) -> tuple[int, list]:
    """Ask freerouter, with the USER's own key, what that key can do and what it is capped at.

    GET /api/v1/keys is scoped to the caller, so authenticating with the user key both proves
    the key still works (200 vs 401) and returns freerouter's OWN answer about the account's
    keys and their limits — the independent source of truth for every cap asserted below. The
    control plane never sees these numbers except through freerouter.

    The listing carries TWO keys per sub-account, and finding that out is why this is read
    from the real binary: `POST /api/v1/subaccounts` returns a root bearer for the new
    account, and that bearer is a key, with NO limit. `generate_key` never hands it to anyone
    and never stores it, but it is in the account's key set — so "what is the user's cap"
    has to be asked of the specific minted hash, not of the account's keys as a whole.
    """
    import httpx

    r = httpx.get(
        f"{base}/api/v1/keys", headers={"Authorization": f"Bearer {key}"}, timeout=10.0
    )
    if r.status_code != 200:
        return r.status_code, []
    return 200, r.json().get("data", [])


def _limit_of(keys: list, key_hash: str):
    """freerouter's reported cap on ONE key, addressed by the hash the control plane recorded."""
    matched = [k for k in keys if k.get("hash") == key_hash]
    assert len(matched) == 1, f"{key_hash} not found in {[k.get('hash') for k in keys]}"
    return matched[0]["limit"]


def _seed_alice_chat_on_freerouter(world, budget: float = 25.0) -> dict:
    """One mirrored (user, surface) whose USER KEY the test still holds.

    `mirror()` drops the minted bearer on the floor by design, so a test that needs to prove
    the OLD key stops working has to mint through the same selector itself and record the
    mirror row exactly as the mirror would. Everything else in SEED is mirrored normally.
    """
    created = run(provisioning.generate_key(
        username="alice", surface="chat", idp_user_id="idp-alice", max_budget=budget,
    ))
    run(_insert_mirror_row(
        world["dsn"], "alice::chat", created["token"], created.get("key_hash"),
        budget, created.get("limit_usd"),
    ))
    run(mirror.mirror())  # the other four live keys
    return created


def test_admin_budget_on_freerouter_applies_the_new_cap_instead_of_500(world):
    """The done-condition, end to end: /admin/budget answers, and the cap it reports is the
    one freerouter is actually enforcing on the key the user now holds."""
    from app.main import BudgetRequest, set_budget

    old = _seed_alice_chat_on_freerouter(world, budget=25.0)
    status, keys = _key_state(world["base"], old["key"])
    assert status == 200 and _limit_of(keys, old["key_hash"]) == 25.0

    result = run(set_budget(BudgetRequest(username="alice", surface="chat", max_budget=99.0)))

    assert result["updated"] == 1 and result["max_budget"] == 99.0
    assert [r["key_alias"] for r in result["rotated"]] == ["alice::chat"]
    new_key = result["rotated"][0]["key"]
    assert new_key != old["key"]

    # freerouter's own answer about the cap on the key the user is now holding, addressed by
    # the hash the ledger recorded for it.
    new_hash = run(_mirror_rows(world["dsn"]))["alice::chat"]["key_hash"]
    status, keys = _key_state(world["base"], new_key)
    assert status == 200, "the replacement key must authenticate"
    assert _limit_of(keys, new_hash) == 99.0

    # ...and the superseded key can no longer spend at the old cap.
    assert _key_state(world["base"], old["key"])[0] == 401


def test_admin_budget_repoints_the_ledger_and_the_mirror_at_the_live_account(world):
    """The continuity half. A budget change that left `gateway_token_hash` naming the retired
    account (or, worse, the LiteLLM hash the mirror deliberately never overwrote) would make
    the NEXT budget change fail against a key the gateway no longer has — silently, from the
    operator's side. That is the failure issuance.py's five steps exist to prevent."""
    from app.main import BudgetRequest, set_budget

    old = _seed_alice_chat_on_freerouter(world, budget=25.0)
    before = {alias: row for alias, row in run(_mirror_rows(world["dsn"])).items()}
    assert before["alice::chat"]["account_id"] == old["token"]
    ledger_before = dict(
        (alias, handle) for alias, handle, _b, _s in run(_virtual_keys(world["dsn"]))
    )
    assert ledger_before["alice::chat"] == "litellm-hash-alice-chat"

    run(set_budget(BudgetRequest(username="alice", surface="chat", max_budget=99.0)))

    after = run(_mirror_rows(world["dsn"]))
    new_account = after["alice::chat"]["account_id"]
    assert new_account != old["token"]
    assert after["alice::chat"]["limit_usd"] == 99
    assert float(after["alice::chat"]["source_max_budget"]) == 99.0
    ledger_after = dict(
        (alias, (handle, float(budget) if budget is not None else None))
        for alias, handle, budget, _s in run(_virtual_keys(world["dsn"]))
    )
    assert ledger_after["alice::chat"] == (new_account, 99.0)
    # Untouched surfaces keep the handle they had — this is one key's rotation, not a reprovision.
    assert ledger_after["alice::ide"][0] == "litellm-hash-alice-ide"

    # The alias now resolves to the LIVE account, so a later revoke lands on the live key.
    assert run(freerouter._account_ids_by_alias())["alice::chat"] == new_account

    # And the reconcile is green: before this item, a budget change on the freerouter path
    # could only ever create permanent `budget_mismatch` drift, because nothing could move
    # freerouter's cap. This is the same check the flip gate reads.
    report = run(mirror.reconcile())
    assert report["budget_mismatch"] == []
    assert report["account_id_conflict"] == []
    assert report["ok"] is True


def test_admin_budget_without_a_surface_rotates_every_live_key(world):
    """No surface means all of them, and every one must end up capped — a partial application
    would leave some surfaces enforcing the old number with the ledger claiming the new one."""
    from app.main import BudgetRequest, set_budget

    _seed_alice_chat_on_freerouter(world, budget=25.0)

    result = run(set_budget(BudgetRequest(username="alice", surface=None, max_budget=7.0)))

    assert result["updated"] == 3  # chat, ide, terminal; carol's revoked key is not alice's
    assert sorted(r["key_alias"] for r in result["rotated"]) == [
        "alice::chat", "alice::ide", "alice::terminal",
    ]
    rows = run(_mirror_rows(world["dsn"]))
    for rotated in result["rotated"]:
        status, keys = _key_state(world["base"], rotated["key"])
        assert status == 200, rotated["key_alias"]
        assert _limit_of(keys, rows[rotated["key_alias"]]["key_hash"]) == 7.0, rotated["key_alias"]
    assert run(mirror.reconcile())["ok"] is True


def test_admin_budget_refuses_a_zero_cap_and_leaves_the_key_alone(world):
    """freerouter reads limit<=0 as UNLIMITED, so applying a zero budget would hand the user
    an uncapped key at the moment an operator tried to stop them spending. Refuse, and change
    nothing — not the key, not the ledger."""
    from fastapi import HTTPException

    from app.main import BudgetRequest, set_budget

    old = _seed_alice_chat_on_freerouter(world, budget=25.0)

    with pytest.raises(HTTPException) as exc:
        run(set_budget(BudgetRequest(username="alice", surface="chat", max_budget=0)))
    assert exc.value.status_code == 400

    status, keys = _key_state(world["base"], old["key"])
    assert status == 200 and _limit_of(keys, old["key_hash"]) == 25.0
    ledger = dict((alias, float(b)) for alias, _h, b, _s in run(_virtual_keys(world["dsn"])) if b is not None)
    assert ledger["alice::chat"] == 25.0


def test_admin_budget_on_a_handle_freerouter_cannot_name_is_a_409_not_a_500(world):
    """Break it the way it actually breaks: the fault is injected through the DATA the
    endpoint reads. With no mirror row, the ledger's handle is still the LiteLLM hash and
    there is no freerouter account it can name — the operator must be told that, not handed a
    stack trace, and nothing must be minted under a guessed alias."""
    from fastapi import HTTPException

    from app.main import BudgetRequest, set_budget

    _seed_alice_chat_on_freerouter(world, budget=25.0)
    run(_delete_mirror_row(world["dsn"], "alice::ide"))

    with pytest.raises(HTTPException) as exc:
        run(set_budget(BudgetRequest(username="alice", surface="ide", max_budget=50.0)))
    assert exc.value.status_code == 409
    assert "alice::ide" in str(exc.value.detail)

    # Control: the SAME endpoint, same freerouter, on the surface whose mirror row is intact.
    assert run(set_budget(
        BudgetRequest(username="alice", surface="chat", max_budget=50.0)
    ))["updated"] == 1


def test_a_rotated_alias_with_spend_is_a_retired_account_not_a_conflict(world):
    """The rollup's ONLY test with real rows in it, and the two claims that need them.

    A budget change rotates the sub-account, and freerouter's revoke keeps the retired
    account's bill — so from then on the rollup reports TWO accounts under one alias, forever.
    Two things must survive that, and neither was reachable before this file could put real
    events in the meter (nothing in this suite can spend: there is no upstream provider):

      1. the reconcile must corroborate the LIVE account rather than calling the alias an
         `account_id_conflict` — which, when the rollup was collapsed to one id per alias,
         happened or did not happen purely by dict order;
      2. the alias must still resolve to the live account, so a later revoke kills the key
         the user is holding and not the one that is already dead.
    """
    from app.main import BudgetRequest, set_budget

    old = _seed_alice_chat_on_freerouter(world, budget=25.0)
    _record_spend(old["token"])  # the account has a bill before the budget changes
    assert run(freerouter.rollup_accounts_by_alias())["alice::chat"] == [old["token"]]

    run(set_budget(BudgetRequest(username="alice", surface="chat", max_budget=99.0)))
    new_account = run(_mirror_rows(world["dsn"]))["alice::chat"]["account_id"]
    _record_spend(new_account)

    # freerouter now reports both, and says so.
    assert sorted(run(freerouter.rollup_accounts_by_alias())["alice::chat"]) == sorted(
        [old["token"], new_account]
    )

    report = run(mirror.reconcile())
    assert report["account_id_conflict"] == []
    assert "alice::chat" in report["confirmed"]
    assert report["retired_accounts"] == [
        {"key_alias": "alice::chat", "live": new_account, "retired": [old["token"]]}
    ]
    assert report["ok"] is True

    # And the alias resolves to the live account even though the dead one is in the rollup.
    assert run(freerouter._account_ids_by_alias())["alice::chat"] == new_account


def test_admin_budget_on_litellm_patches_in_place_and_never_rotates(world, monkeypatch):
    """Behaviour preservation, which is the standing constraint on this whole cutover.

    The LiteLLM server is the one thing stubbed, and only at the TRANSPORT: app/gateway.py's
    real request code runs (its URL, body, headers and raise_for_status), against an
    httpx.MockTransport standing in for a LiteLLM that this suite has never run — the same
    call the `--verify-litellm` test makes and for the same reason. What is under test is
    main.set_budget's backend selection, and none of it is stubbed: it must address LiteLLM by
    the LEDGER's hash (never the freerouter account id, which exists here), must not rotate,
    and must leave every freerouter-side record untouched.
    """
    import httpx

    from app.main import BudgetRequest, set_budget

    _seed_alice_chat_on_freerouter(world, budget=25.0)
    mirror_before = run(_mirror_rows(world["dsn"]))

    seen: list[dict] = []

    def litellm(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/key/update":
            return httpx.Response(404, json={"error": f"unhandled {request.url.path}"})
        body = json.loads(request.content)
        seen.append(body)
        return httpx.Response(200, json={"key": body["key"], "max_budget": body["max_budget"]})

    real_client = httpx.AsyncClient

    def client_with_mock(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(litellm)
        return real_client(*args, **kwargs)

    monkeypatch.setenv("GATEWAY_PROVIDER", "litellm")
    monkeypatch.setenv("GATEWAY_MASTER_KEY", "sk-master")
    monkeypatch.setattr(gateway.httpx, "AsyncClient", client_with_mock)
    assert provisioning.backend() is gateway

    result = run(set_budget(BudgetRequest(username="alice", surface="chat", max_budget=99.0)))

    assert result["updated"] == 1 and result["rotated"] == []
    # Addressed by the LiteLLM hash the ledger holds, not by the freerouter account id.
    assert seen == [{"key": "litellm-hash-alice-chat", "max_budget": 99.0}]
    ledger = dict(
        (alias, (h, float(b) if b is not None else None))
        for alias, h, b, _s in run(_virtual_keys(world["dsn"]))
    )
    assert ledger["alice::chat"] == ("litellm-hash-alice-chat", 99.0)
    # The freerouter side is not a participant on this backend and must not have moved.
    assert run(_mirror_rows(world["dsn"])) == mirror_before


# ---------------------------------------------------------------- the script


def test_the_reconcile_script_exits_nonzero_when_the_cutover_is_not_safe(world):
    """`python -m app.mirror --reconcile` as a real subprocess: the exit status IS the gate."""
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT),
        "CONTROL_PLANE_DATABASE_URL": world["dsn"],
        "FREEROUTER_MASTER_KEY": os.environ["FREEROUTER_MASTER_KEY"],
        "FREEROUTER_URL": world["base"],
        "GATEWAY_PROVIDER": "freerouter",
    }

    green = subprocess.run(
        [sys.executable, "-m", "app.mirror", "--mirror", "--reconcile"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=120,
    )
    assert green.returncode == 0, f"{green.stdout}\n{green.stderr}"
    report = json.loads(green.stdout)["reconcile"]
    assert report["missing"] == [] and report["budget_mismatch"] == []

    run(_delete_mirror_row(world["dsn"], "alice::chat"))
    red = subprocess.run(
        [sys.executable, "-m", "app.mirror", "--reconcile"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=120,
    )
    assert red.returncode == 1, f"{red.stdout}\n{red.stderr}"
    assert json.loads(red.stdout)["reconcile"]["missing"] == ["alice::chat"]


# ---------------------------------------------------------------- direct database reads
#
# Deliberately their OWN connection rather than app.mirror's readers: a test that checked the
# mirror by calling the mirror would pass on any self-consistent nonsense.


async def _mirror_rows(dsn) -> dict:
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            "SELECT key_alias, account_id, key_hash, source_max_budget, limit_usd "
            "FROM freerouter_mirror"
        )
    finally:
        await conn.close()
    return {r["key_alias"]: dict(r) for r in rows}


async def _virtual_keys(dsn) -> list:
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            "SELECT key_alias, gateway_token_hash, max_budget, status "
            "FROM virtual_key ORDER BY key_alias"
        )
    finally:
        await conn.close()
    return [tuple(r) for r in rows]


async def _insert_mirror_row(
    dsn, alias, account_id, key_hash, source_max_budget=None, limit_usd=None
) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            "INSERT INTO freerouter_mirror "
            "(key_alias, account_id, key_hash, source_max_budget, limit_usd) "
            "VALUES ($1, $2, $3, $4, $5)",
            alias, account_id, key_hash, source_max_budget, limit_usd,
        )
    finally:
        await conn.close()


async def _delete_mirror_row(dsn, alias) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("DELETE FROM freerouter_mirror WHERE key_alias = $1", alias)
    finally:
        await conn.close()


async def _set_budget(dsn, alias, budget) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            "UPDATE virtual_key SET max_budget = $2 WHERE key_alias = $1", alias, budget
        )
    finally:
        await conn.close()


async def _revoke_virtual_key(dsn, alias) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            "UPDATE virtual_key SET status = 'revoked', revoked_at = now() "
            "WHERE key_alias = $1",
            alias,
        )
    finally:
        await conn.close()
