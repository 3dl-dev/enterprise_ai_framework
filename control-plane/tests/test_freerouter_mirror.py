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

from app import db, freerouter, gateway, mirror, provisioning  # noqa: E402

FR_BINARY = Path(os.environ.get("FREEROUTER_BINARY", "/tmp/fr-enterpriseaiframework-1f8"))
FR_SOURCE = Path(os.environ.get("FREEROUTER_SOURCE", "/home/baron/projects/freerouter"))


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


async def _insert_mirror_row(dsn, alias, account_id, key_hash) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            "INSERT INTO freerouter_mirror (key_alias, account_id, key_hash) "
            "VALUES ($1, $2, $3)",
            alias, account_id, key_hash,
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
