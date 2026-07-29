"""A request that was served is on the bill, even if the gateway stops one second later.

THE DEFECT THIS EXISTS FOR, measured on the bundle rather than reasoned about
(`enterpriseaiframework-3f3`). A request was served from a workspace pod on its own
`baron::ide` key on 2026-07-28 at 02:10:50Z — HTTP 200 — and never appeared in the spend
ledger. Two explanations were disproved first: it was not a cache hit (cache hits write
their own row) and it was not the master key (master-key rows do land, as
`(unattributed)`, and there were none in that hour). The row was not orphaned from a join
either. It was never written.

Reproduced here, deterministically, on the compose bundle: serve a request, `docker compose
stop gateway`, bring it back, wait past every flush interval — the row is gone for good,
while a control request in the same run bills normally. LiteLLM appends spend rows to an
in-memory list and commits them from a scheduled job every 7-13 seconds; its shutdown event
disconnects the database without draining that list. So the loss is not a crash-only edge
case: every ordinary redeploy silently drops billing for requests already served and
already charged upstream. Full reasoning: deploy/gateway/flush_spend_on_shutdown.py.

WHY THERE ARE TWO TESTS AND NOT ONE.

The end-to-end test is the one that would have caught the defect, but on its own it can
pass for the wrong reason: if the scheduler happens to tick between the response and the
stop, the row is written by the mechanism that was always there and the restart proves
nothing. So it explicitly establishes that at least one request was still buffered when
the gateway was stopped, and retries only that precondition — never the assertion.

The ordering test carries no timing at all. It exercises the wrapper inside the real
gateway container with the flush and the vendor's shutdown both replaced by recorders, and
asserts the flush runs *first*. Ordering is the entire defect: LiteLLM's own shutdown does
disconnect, it just does it before the batch is committed.
"""

import csv
import io
import subprocess
import time
import uuid

import httpx
import pytest

from conftest import BUNDLE, compose

TIMEOUT = 60
pytestmark = pytest.mark.usefixtures("stack_up")

GENERATED_CONFIG = BUNDLE / "litellm" / "config.generated.yaml"


def _ledger(control_plane_url, admin_headers) -> dict[str, dict]:
    """The ledger as the product renders it, keyed by request id.

    Read through /admin/export/spend rather than by reaching into postgres: the point is
    what the bill and the exit archive say, and a test that queries the database directly
    would keep passing if the rendering lost the row on the way out.
    """
    r = httpx.get(
        f"{control_plane_url}/admin/export/spend", headers=admin_headers, timeout=120
    )
    assert r.status_code == 200, r.text
    return {row["request_id"]: row for row in csv.DictReader(io.StringIO(r.text))}


def _serve(gateway_url, headers) -> tuple[str, int]:
    """One real request. Returns (request_id, total_tokens) as the caller was told them."""
    r = httpx.post(
        f"{gateway_url}/v1/chat/completions",
        headers=headers,
        json={"model": "fake-large",
              "messages": [{"role": "user", "content": f"restart probe {uuid.uuid4().hex}"}]},
        timeout=TIMEOUT,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["usage"]["total_tokens"] > 0, body
    return body["id"], body["usage"]["total_tokens"]


def _gateway_healthy(deadline: float) -> bool:
    """A gateway that answers is not the same as one compose calls healthy.

    Leaving the container in `starting` leaks into every later test that asserts on stack
    health, so a test that stops the gateway has to put it back properly.
    """
    while time.monotonic() < deadline:
        ps = compose("ps", "--format", "{{.Service}} {{.Health}}")
        for line in ps.stdout.strip().splitlines():
            parts = line.split()
            if parts and parts[0] == "gateway" and parts[-1] == "healthy":
                return True
        time.sleep(3)
    return False


def test_the_gateway_config_still_names_the_shutdown_flush():
    """Guard the guard. Dropping the callback silently reopens the hole.

    tests/test_gateway_callbacks.py proves that whatever the config names is shipped by
    every deployment; it cannot notice a module that stopped being named at all.
    """
    text = GENERATED_CONFIG.read_text()
    assert "flush_spend_on_shutdown.handler" in text, (
        "config.generated.yaml no longer loads flush_spend_on_shutdown. Without it the "
        "gateway disconnects its database with spend rows still buffered in memory, and "
        "every restart loses the billing for requests it already served. If that removal "
        "was deliberate, delete this assertion deliberately too."
    )


def test_the_flush_runs_before_the_proxy_disconnects_its_database():
    """The ordering property, asserted in the real container, with no timing in it.

    Both halves of the wrapper are replaced by recorders, so this cannot pass by the
    scheduler happening to run: it fails if the flush is skipped, and it fails if the
    flush is merely called after the vendor's shutdown — which is the defect exactly.
    """
    cid = compose("ps", "-q", "gateway").stdout.strip()
    assert cid, "gateway container is not running"

    script = """
import asyncio
import litellm.proxy.proxy_server as ps
import litellm.proxy.utils as u

calls = []

async def fake_update_spend(**kwargs):
    calls.append("flush")

async def fake_shutdown():
    calls.append("vendor-shutdown")

# Patched BEFORE the import: the module resolves update_spend at install time and wraps
# whatever proxy_shutdown_event is then, which is the real deployment path.
u.update_spend = fake_update_spend
ps.proxy_shutdown_event = fake_shutdown

import flush_spend_on_shutdown as f

# Installing twice must not flush twice — the module is imported once per callback entry
# and a double flush would mean a double spend commit.
f._flush_before_disconnect()

ps.prisma_client = object()
ps.db_writer_client = None
ps.proxy_logging_obj = object()
asyncio.run(ps.proxy_shutdown_event())
print("ORDER=" + ",".join(calls))
"""
    probe = subprocess.run(
        ["docker", "exec", cid, "python", "-c", script],
        capture_output=True, text=True, timeout=180,
    )
    assert probe.returncode == 0, (
        f"could not exercise the shutdown wrapper in the gateway container: "
        f"{probe.stderr[-800:]}"
    )
    order = next(
        (ln.split("=", 1)[1] for ln in probe.stdout.splitlines() if ln.startswith("ORDER=")),
        None,
    )
    assert order == "flush,vendor-shutdown", (
        f"pending spend rows are not committed before the proxy tears its database down "
        f"(call order was {order!r}). LiteLLM's own shutdown event disconnects Prisma and "
        f"flushes Langfuse but never drains prisma_client.spend_log_transactions, so "
        f"anything served in the last 7-13 seconds is lost."
    )


def test_a_served_request_is_still_on_the_bill_after_an_ordinary_gateway_stop(
    gateway_url, control_plane_url, admin_headers, named_key_headers
):
    """Ground truth, end to end, through the rendering a customer actually reads."""
    served: list[tuple[str, int]] = []
    buffered: list[str] = []

    # Establish the precondition: at least one request still sitting in memory when the
    # gateway is asked to stop. Retried because the scheduler ticks on its own every 7-13
    # seconds and may empty the buffer under us; the assertions below are never retried.
    for _ in range(3):
        served = [_serve(gateway_url, named_key_headers) for _ in range(3)]
        on_bill = _ledger(control_plane_url, admin_headers)
        buffered = [rid for rid, _ in served if rid not in on_bill]
        if buffered:
            break
    else:
        pytest.fail(
            "nine requests in a row were committed to the ledger before the gateway was "
            "even asked to stop, so this test cannot tell a working shutdown flush from a "
            "missing one. Either litellm now writes spend rows inline — in which case "
            "verify that and remove this test deliberately — or the batch interval changed."
        )

    stopped = compose("stop", "gateway")
    assert stopped.returncode == 0, stopped.stderr

    on_bill = _ledger(control_plane_url, admin_headers)
    try:
        # THE ASSERTION. Note it is made while the gateway is still down: there is no
        # second chance for a scheduler tick to cover for a shutdown that dropped the rows.
        missing = [rid for rid, _ in served if rid not in on_bill]
        assert not missing, (
            f"{len(missing)} of {len(served)} requests were served with HTTP 200 and are "
            f"not on the bill after an ordinary `compose stop gateway` "
            f"({len(buffered)} of them were still buffered at stop time). The tokens were "
            f"bought and charged upstream and nobody will ever be billed for them: "
            f"{missing}"
        )

        # A row existing is not a row being right. This is the failure mode the project
        # keeps hitting — presence checks that pass for years over wrong content.
        for rid, tokens in served:
            row = on_bill[rid]
            assert int(row["total_tokens"]) == tokens, (
                f"{rid}: the caller was told {tokens} tokens, the bill says "
                f"{row['total_tokens']}"
            )
            assert float(row["spend"]) > 0, (
                f"{rid}: served {tokens} tokens against a priced model and recorded "
                f"spend={row['spend']} — a row that bills nothing is not billing"
            )
            assert row["principal"] and row["principal"] != "(unattributed)", (
                f"{rid}: flushed at shutdown but with no principal ({row['principal']!r}); "
                f"the alias is stamped at request time and must survive the flush"
            )
            assert row["surface"] == "terminal", (
                f"{rid}: surface came back {row['surface']!r}, not the 'terminal' encoded "
                f"in the key alias the request was made with"
            )
    finally:
        compose("up", "-d", "gateway", check=False)
        assert _gateway_healthy(time.monotonic() + 180), (
            "gateway never returned to healthy after this test stopped it"
        )

    # And the path this change did not touch still works: with no restart at all, a request
    # bills exactly as before. A shutdown hook that broke ordinary metering would otherwise
    # be invisible here.
    rid, tokens = _serve(gateway_url, named_key_headers)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        row = _ledger(control_plane_url, admin_headers).get(rid)
        if row:
            break
        time.sleep(3)
    else:
        pytest.fail(f"{rid} was served but never billed with no restart involved at all")
    assert int(row["total_tokens"]) == tokens, row
    assert float(row["spend"]) > 0, row
    assert row["surface"] == "terminal", row
