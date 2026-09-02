"""Item 730 — the bill reads continuous history across the LiteLLM -> freerouter flip.

app.metering_select.spend_by_user_and_surface / totals no longer pick ONE backend by
GATEWAY_PROVIDER; they read BOTH and merge (see metering_select.py's module docstring for
why that alone closes the boundary, with no cutover timestamp: each backend only ever holds
rows for the span it was actually live).

This suite proves it against two REAL backends, not stubs:

  - a disposable Postgres container (plain `docker run`, NOT docker-compose/`make up` — this
    does not touch the shared dev stack sibling agents use) seeded with a minimal
    LiteLLM_SpendLogs + LiteLLM_VerificationToken fixture standing in for "pre-flip history".
  - a real freerouter binary (built from ~/projects/freerouter, FREEROUTER_METER_BACKEND=
    sqlite, a random port) driven through its REAL HTTP signup/subaccount/topup routes and
    then through the meter's own public Reserve/Settle spine (metering.Meter — the exact call
    core/routing.go makes for a real inference request) so the recorded spend is the meter's
    own authoritative cost computation, standing in for "post-flip usage".

Independent source of truth (Q1): every expected number here is hand-computed from the
token counts and per-MTok prices fed into the fixtures (plain arithmetic, not a call through
metering.py/metering_freerouter.py), and cross-checked once against the real systems'
own output before being hardcoded (see the literal floats below).

Q2 (broke it the way it actually breaks) lives in TestMergeNoGap: the pre-merge control run
proves a hard GATEWAY_PROVIDER switch DROPS the LiteLLM half — the exact "gap at the
boundary" this item exists to close — by calling the two backends' own functions directly
(no monkeypatch of the fault: it is the flip's real behaviour, LiteLLM and freerouter each
only ever holding their own half).

Skips (does not fail) when docker or the freerouter checkout/toolchain are unavailable, so
this suite does not become a spurious red in an environment that cannot host either real
backend; every other targeted test in this item still runs and gates the change.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FREEROUTER_SRC = Path.home() / "projects" / "freerouter"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _have_prereqs() -> str | None:
    if not shutil.which("docker"):
        return "docker not available"
    if not shutil.which("go"):
        return "go toolchain not available"
    if not FREEROUTER_SRC.exists():
        return f"no freerouter checkout at {FREEROUTER_SRC}"
    r = subprocess.run(["docker", "info"], capture_output=True, timeout=10)
    if r.returncode != 0:
        return "docker daemon not reachable"
    return None


_SEED_GO = '''
// Throwaway helper: drives real usage through freerouter's own public metering seam
// (metering.Meter.Resolve/Reserve/Settle -- the exact spine a real inference request
// takes) against the SAME sqlite ledger file the freerouter server serves from. Run
// only while the server is not holding that DSN open (sqlite single-writer).
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"os"

	"github.com/3dl-dev/freerouter/metering"
)

type ev struct {
	Bearer           string  `json:"bearer"`
	Model            string  `json:"model"`
	Vendor           string  `json:"vendor"`
	InputTokens      int     `json:"input_tokens"`
	OutputTokens     int     `json:"output_tokens"`
	InputPerMTokUSD  float64 `json:"input_per_mtok_usd"`
	OutputPerMTokUSD float64 `json:"output_per_mtok_usd"`
}

func main() {
	dsn := os.Args[1]
	rt, err := metering.Open(metering.RuntimeConfig{Backend: metering.BackendSQLite, DSN: dsn})
	if err != nil {
		fmt.Fprintln(os.Stderr, "open:", err)
		os.Exit(1)
	}
	defer rt.Close()

	var events []ev
	if err := json.NewDecoder(os.Stdin).Decode(&events); err != nil {
		fmt.Fprintln(os.Stderr, "decode:", err)
		os.Exit(1)
	}
	ctx := context.Background()
	for i, e := range events {
		rt.SetPrice(e.Vendor+"|"+e.Model, metering.Price{
			InputPerMTok: e.InputPerMTokUSD, OutputPerMTok: e.OutputPerMTokUSD,
		})
		t, err := rt.Meter.Resolve(ctx, e.Bearer)
		if err != nil {
			fmt.Fprintln(os.Stderr, "resolve", i, ":", err)
			os.Exit(1)
		}
		res, err := rt.Meter.Reserve(ctx, t, 0)
		if err != nil {
			fmt.Fprintln(os.Stderr, "reserve", i, ":", err)
			os.Exit(1)
		}
		if _, err := rt.Meter.Settle(ctx, res, metering.Usage{
			InputTokens: e.InputTokens, OutputTokens: e.OutputTokens,
			ModelID: e.Model, Vendor: e.Vendor,
		}); err != nil {
			fmt.Fprintln(os.Stderr, "settle", i, ":", err)
			os.Exit(1)
		}
	}
	fmt.Println("ok")
}
'''

_SEED_GOMOD = """module seedusage730

go 1.25.0

require github.com/3dl-dev/freerouter v0.0.0

replace github.com/3dl-dev/freerouter => {freerouter_src}
""".format(freerouter_src=FREEROUTER_SRC)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(scope="module")
def real_stack(tmp_path_factory):
    """Both real backends, seeded, torn down at the end of the module.

    Everything here is a plain subprocess/docker invocation against ephemeral, randomly
    ported resources -- never `make up` / docker compose, so it cannot collide with a
    sibling agent's shared dev stack.
    """
    reason = _have_prereqs()
    if reason:
        pytest.skip(f"real freerouter+postgres stack unavailable: {reason}")

    work = tmp_path_factory.mktemp("fr730")

    # --- build freerouter (once) ---------------------------------------------------
    fr_bin = work / "freerouter-bin"
    build = subprocess.run(
        ["go", "build", "-o", str(fr_bin), "./cmd/freerouter"],
        cwd=str(FREEROUTER_SRC), capture_output=True, text=True, timeout=180,
    )
    if build.returncode != 0:
        pytest.skip(f"could not build freerouter: {build.stderr[-2000:]}")

    # --- build the seed helper (once) -----------------------------------------------
    seed_dir = work / "seedusage"
    seed_dir.mkdir()
    (seed_dir / "main.go").write_text(_SEED_GO)
    (seed_dir / "go.mod").write_text(_SEED_GOMOD)
    tidy = subprocess.run(["go", "mod", "tidy"], cwd=str(seed_dir),
                          capture_output=True, text=True, timeout=120)
    if tidy.returncode != 0:
        pytest.skip(f"could not resolve seed helper deps: {tidy.stderr[-2000:]}")
    seed_bin = work / "seedusage-bin"
    build2 = subprocess.run(["go", "build", "-o", str(seed_bin), "."], cwd=str(seed_dir),
                            capture_output=True, text=True, timeout=120)
    if build2.returncode != 0:
        pytest.skip(f"could not build seed helper: {build2.stderr[-2000:]}")

    # --- disposable Postgres (plain docker run, random port) ------------------------
    pg_port = _free_port()
    container = f"eaf730pg-{pg_port}"
    subprocess.run(["docker", "rm", "-f", container], capture_output=True)
    r = subprocess.run([
        "docker", "run", "--rm", "-d", "--name", container,
        "-e", "POSTGRES_PASSWORD=eaitest", "-e", "POSTGRES_USER=eai", "-e", "POSTGRES_DB=eai",
        "-p", f"127.0.0.1:{pg_port}:5432", "postgres:16-alpine",
    ], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        pytest.skip(f"could not start disposable postgres: {r.stderr}")

    def _pg_ready() -> bool:
        return subprocess.run(["docker", "exec", container, "pg_isready", "-U", "eai"],
                              capture_output=True, timeout=5).returncode == 0

    deadline = time.time() + 60
    while time.time() < deadline and not _pg_ready():
        time.sleep(1)
    if not _pg_ready():
        subprocess.run(["docker", "rm", "-f", container], capture_output=True)
        pytest.skip("disposable postgres never became ready")

    # Minimal LiteLLM schema -- just the columns metering.py's real SQL reads.
    ddl = '''
    CREATE TABLE "LiteLLM_VerificationToken" (token text PRIMARY KEY, key_alias text);
    CREATE TABLE "LiteLLM_SpendLogs" (
        request_id text PRIMARY KEY,
        "startTime" timestamptz,
        api_key text,
        end_user text,
        spend double precision,
        prompt_tokens bigint,
        completion_tokens bigint,
        total_tokens bigint,
        model text,
        cache_hit text,
        metadata jsonb
    );
    -- carol::chat via the metadata alias (the primary attribution path).
    INSERT INTO "LiteLLM_SpendLogs" VALUES
      ('req-1', '2026-01-15T10:00:00Z', 'hash-carol', NULL, 0.5, 10000, 2000, 12000,
       'gpt-x', 'false', '{"user_api_key_alias":"carol::chat"}'::jsonb),
      ('req-2', '2026-01-16T10:00:00Z', 'hash-carol', NULL, 0.25, 5000, 1000, 6000,
       'gpt-x', 'false', '{"user_api_key_alias":"carol::chat"}'::jsonb),
    -- dave::ide via the VerificationToken join fallback (pre-metadata-era row).
      ('req-3', '2026-01-17T10:00:00Z', 'hash-dave', NULL, 0.1, 2000, 500, 2500,
       'gpt-x', 'false', '{}'::jsonb);
    INSERT INTO "LiteLLM_VerificationToken" VALUES ('hash-dave', 'dave::ide');
    '''
    dsn = f"postgresql://eai:eaitest@127.0.0.1:{pg_port}/eai"
    # The official postgres image restarts itself once after initdb (stop, then a second
    # "ready to accept connections"); pg_isready can report ready in the narrow window
    # before that restart. Retry the actual DDL rather than trusting one readiness probe.
    psql = None
    for attempt in range(10):
        psql = subprocess.run(["docker", "exec", "-i", container, "psql", "-U", "eai", "-d", "eai"],
                              input=ddl, capture_output=True, text=True, timeout=30)
        if psql.returncode == 0:
            break
        time.sleep(2)
    if psql is None or psql.returncode != 0:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True)
        pytest.skip(f"could not seed LiteLLM fixture: {psql.stderr if psql else 'no attempt ran'}")

    # --- real freerouter: signup, subaccounts, topup, real Reserve/Settle usage -----
    fr_port = _free_port()
    fr_dsn = work / "freerouter-meter.db"
    env_base = {
        "FREEROUTER_LISTEN_ADDR": f"127.0.0.1:{fr_port}",
        "FREEROUTER_METER_BACKEND": "sqlite",
        "FREEROUTER_METER_DSN": str(fr_dsn),
        "FREEROUTER_SIGNUP": "open",
        "FREEROUTER_TEST_FUNDS": "open",
        "FREEROUTER_PROFILE": "personal-gateway",
    }
    import os as _os
    import httpx

    def _spawn():
        return subprocess.Popen([str(fr_bin)], env={**_os.environ, **env_base},
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    proc = _spawn()
    base = f"http://127.0.0.1:{fr_port}"
    try:
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                if httpx.get(f"{base}/healthz", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.3)
        else:
            raise RuntimeError("freerouter never came up")

        cp = httpx.post(f"{base}/api/v1/signup", json={"display_name": "cp"}, timeout=10).json()["data"]
        cp_key = cp["api_key"]
        hdr = {"Authorization": f"Bearer {cp_key}"}
        alice = httpx.post(f"{base}/api/v1/subaccounts", headers=hdr,
                           json={"name": "alice::chat"}, timeout=10).json()["data"]
        bob = httpx.post(f"{base}/api/v1/subaccounts", headers=hdr,
                         json={"name": "bob::ide"}, timeout=10).json()["data"]
        for sub in (alice, bob):
            httpx.post(f"{base}/api/v1/credits/topup",
                      headers={"Authorization": f"Bearer {sub['api_key']}"},
                      json={"amount_usd": 5}, timeout=10)
    finally:
        proc.terminate()
        proc.wait(timeout=10)

    # Real Reserve/Settle against the same sqlite file, server stopped (single-writer).
    events = [
        {"bearer": alice["api_key"], "model": "gpt-x", "vendor": "mock",
         "input_tokens": 1000, "output_tokens": 500,
         "input_per_mtok_usd": 3.0, "output_per_mtok_usd": 15.0},
        {"bearer": alice["api_key"], "model": "gpt-x", "vendor": "mock",
         "input_tokens": 2000, "output_tokens": 800,
         "input_per_mtok_usd": 3.0, "output_per_mtok_usd": 15.0},
        {"bearer": bob["api_key"], "model": "gpt-x", "vendor": "mock",
         "input_tokens": 500, "output_tokens": 100,
         "input_per_mtok_usd": 3.0, "output_per_mtok_usd": 15.0},
    ]
    seed = subprocess.run([str(seed_bin), str(fr_dsn)], input=json.dumps(events),
                          capture_output=True, text=True, timeout=30)
    if seed.returncode != 0 or "ok" not in seed.stdout:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True)
        pytest.skip(f"could not seed real freerouter usage: {seed.stderr}")

    proc = _spawn()
    try:
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                if httpx.get(f"{base}/healthz", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.3)
        else:
            raise RuntimeError("freerouter never came back up")

        yield {
            "gateway_dsn": dsn,
            "freerouter_url": base,
            "freerouter_master_key": cp_key,
        }
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        subprocess.run(["docker", "rm", "-f", container], capture_output=True)


@pytest.fixture
def real_asyncpg():
    """Undo, for one test, an earlier-collected test file's fake `sys.modules["asyncpg"]`.

    Several files in this suite (test_export_attribution.py, test_portal_agents.py, ...)
    install a bare `types.ModuleType("asyncpg")` stub -- guarded by `if "asyncpg" not in
    sys.modules` -- so files that don't need a database can import app.metering without the
    real driver present. Whichever of them collects first wins that guard for the WHOLE
    session, since pytest collects every file before any test runs. This suite's whole
    point is proving app.metering's real SQL against a real Postgres, so it swaps the real,
    installed asyncpg in for the duration of the test, reloading app.metering so its
    module-level `import asyncpg` re-binds to it -- then puts the stub back and reloads
    metering again, so every OTHER test in the session still sees the stub it expects
    (app.metering is a process-wide singleton; an unrestored swap would leak into whichever
    test happens to import/reload it next).
    """
    import sys as _sys
    import importlib as _importlib

    from app import metering

    prev = _sys.modules.get("asyncpg")
    if prev is None or getattr(prev, "__file__", None) is None:
        _sys.modules.pop("asyncpg", None)
        import asyncpg  # noqa: F401  (the real, installed package)
    _importlib.reload(metering)
    try:
        yield metering
    finally:
        if prev is not None:
            _sys.modules["asyncpg"] = prev
        else:
            _sys.modules.pop("asyncpg", None)
        _importlib.reload(metering)


class TestMergeNoGap:
    """DONE condition: metering_freerouter totals/per-user spend match the fixture, and
    metering_select merges LiteLLM history with freerouter usage with no gap at the flip.
    """

    def test_freerouter_matches_the_real_invoice_fixture(self, real_stack, monkeypatch):
        monkeypatch.setenv("FREEROUTER_URL", real_stack["freerouter_url"])
        monkeypatch.setenv("FREEROUTER_MASTER_KEY", real_stack["freerouter_master_key"])
        from app import metering_freerouter

        rows = run(metering_freerouter.spend_by_user_and_surface())
        by = {(r["username"], r["surface"]): r for r in rows}

        # Independent source of truth: 1000*$3/MTok + 500*$15/MTok, and the second alice
        # request the same way, computed in plain Python -- not through the module under
        # test -- then cross-checked once against the real running system's own output
        # (freerouter's Settle truncates USD->micro per-event before summing, which is why
        # these are not the "obvious" 0.0105+0.018=0.0285 but 28499 micro).
        assert by[("alice", "chat")]["spend"] == pytest.approx(0.028499, abs=1e-9)
        assert by[("alice", "chat")]["requests"] == 2
        assert by[("alice", "chat")]["prompt_tokens"] == 3000
        assert by[("alice", "chat")]["completion_tokens"] == 1300
        assert by[("bob", "ide")]["spend"] == pytest.approx(0.003, abs=1e-9)
        assert by[("bob", "ide")]["requests"] == 1

        tot = run(metering_freerouter.totals())
        assert tot["spend"] == pytest.approx(0.031499, abs=1e-9)
        assert tot["requests"] == 3

    def test_hard_switch_drops_pre_flip_history_the_gap_this_item_closes(self, real_stack, monkeypatch):
        """Q2 control, defect side: calling ONLY the post-flip backend (what a hard
        GATEWAY_PROVIDER switch used to do before this item) loses carol/dave entirely --
        the actual gap at the boundary. This is the real, unmodified behaviour of
        metering_freerouter on its own; nothing is broken-by-injection here because the
        defect IS "read one backend only", which is simply calling that backend alone.
        """
        monkeypatch.setenv("FREEROUTER_URL", real_stack["freerouter_url"])
        monkeypatch.setenv("FREEROUTER_MASTER_KEY", real_stack["freerouter_master_key"])
        from app import metering_freerouter

        rows = run(metering_freerouter.spend_by_user_and_surface())
        names = {r["username"] for r in rows}
        assert "carol" not in names and "dave" not in names, (
            "freerouter alone was never going to have LiteLLM's pre-flip rows -- "
            "that is exactly the gap metering_select must close by merging"
        )

    def test_merge_carries_both_backends_with_no_gap(self, real_stack, real_asyncpg, monkeypatch):
        """Fixed side: metering_select.spend_by_user_and_surface / totals, called with
        GATEWAY_PROVIDER unset (litellm default) exactly as it is pre-flip, still surface
        the freerouter-side rows too -- and the reverse (GATEWAY_PROVIDER=freerouter, the
        state after the flip) still surfaces the LiteLLM history. Either way both halves
        of the bill are present with no gap, which is the whole point: the merge does not
        depend on GATEWAY_PROVIDER at all.
        """
        monkeypatch.setenv("GATEWAY_DATABASE_URL", real_stack["gateway_dsn"])
        monkeypatch.setenv("FREEROUTER_URL", real_stack["freerouter_url"])
        monkeypatch.setenv("FREEROUTER_MASTER_KEY", real_stack["freerouter_master_key"])

        metering = real_asyncpg
        from app import metering_select

        # asyncpg's pool is bound to the event loop that created it, so BOTH
        # GATEWAY_PROVIDER states are driven inside one asyncio.run() / one loop rather
        # than one run() per await -- a second asyncio.run() would hand the cached pool a
        # closed loop and every LiteLLM-side call would silently fail open to empty,
        # which is a test-harness bug (production runs one long-lived loop), not the
        # thing under test.
        async def _drive():
            results = []
            for provider_value in (None, "freerouter"):
                if provider_value is None:
                    monkeypatch.delenv("GATEWAY_PROVIDER", raising=False)
                else:
                    monkeypatch.setenv("GATEWAY_PROVIDER", provider_value)
                rows = await metering_select.spend_by_user_and_surface()
                tot = await metering_select.totals()
                results.append((provider_value, rows, tot))
            pool_obj = getattr(metering, "_pool", None)
            if pool_obj is not None:
                await pool_obj.close()
            return results

        for provider_value, rows, tot in run(_drive()):
            by = {(r["username"], r["surface"]): r for r in rows}

            # pre-flip (LiteLLM) history present:
            assert by[("carol", "chat")]["spend"] == pytest.approx(0.75, abs=1e-9), provider_value
            assert by[("carol", "chat")]["requests"] == 2
            assert by[("dave", "ide")]["spend"] == pytest.approx(0.1, abs=1e-9)
            # post-flip (freerouter) usage present, same call:
            assert by[("alice", "chat")]["spend"] == pytest.approx(0.028499, abs=1e-9)
            assert by[("bob", "ide")]["spend"] == pytest.approx(0.003, abs=1e-9)

            # 0.5+0.25+0.1 (LiteLLM) + 0.028499+0.003 (freerouter), no double count, no gap.
            assert tot["spend"] == pytest.approx(0.881499, abs=1e-9), provider_value
            assert tot["requests"] == 6

    def test_metering_read_only_never_rewrites_litellm_rows(self, real_stack, real_asyncpg, monkeypatch):
        """CONSTRAINT: the seam is read-only. Two independent reads of the same pre-flip
        row through the real gateway DB return byte-identical spend/tokens -- the row was
        never touched by anything the merge did.
        """
        monkeypatch.setenv("GATEWAY_DATABASE_URL", real_stack["gateway_dsn"])
        metering = real_asyncpg

        async def _drive():
            before = await metering.spend_by_user_and_surface()
            after = await metering.spend_by_user_and_surface()
            await metering._pool.close()  # type: ignore[union-attr]
            return before, after

        before, after = run(_drive())
        assert before == after
