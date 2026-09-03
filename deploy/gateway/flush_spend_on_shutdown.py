"""A request that was served is on the bill, even if the gateway stops one second later.

WHAT THIS FIXES, MEASURED RATHER THAN REASONED

`enterpriseaiframework-3f3`: one request, served with HTTP 200 from a workspace pod on its
own `alice::ide` key on 2026-07-28 at 02:10:50Z, never appeared in the spend ledger. Two
earlier explanations were disproved by measurement — it was not a cache hit (cache hits
write their own row) and it was not the master key (master-key rows land in the ledger as
`(unattributed)`; there were none in that hour at all). The row was not orphaned from a
join. It was never written.

The mechanism, reproduced on the compose bundle rather than argued:

  1. `POST /v1/chat/completions` -> HTTP 200, 19 tokens, real content returned.
  2. `docker compose stop gateway` — an ordinary SIGTERM, container down in 2.8s.
  3. Bring it back, wait past every flush interval: **the row is never written.**

The control in the same run — identical request, no restart — bills normally.

WHY, FROM THE VENDOR'S OWN SOURCE

LiteLLM does not write a spend row inline with the response. The logging callback appends
it to `prisma_client.spend_log_transactions`, an in-memory list, and a scheduled job
commits the batch every `random.randint(PROXY_BATCH_WRITE_AT - 3, + 3)` = 7-13 seconds
(`proxy_server.ProxyStartupEvent.initialize_scheduled_background_jobs`). Between the 200
and the next tick the only record of the money is a Python list.

`proxy_shutdown_event` disconnects Prisma, closes the cache, and flushes *Langfuse*. It
never drains that list — it disconnects the database out from under it. So the loss is not
a crash-only edge case: **every ordinary redeploy, rollout or restart of the gateway
silently discards up to thirteen seconds of billing for requests already served and
already charged upstream.** Nothing errors. That is what makes it dangerous: the bill is
not wrong in a way anybody can see, it is short by exactly the traffic nobody can name.

WHAT THIS DOES

Wraps the vendor's shutdown event so the pending batch is committed *before* the database
connection is torn down, using LiteLLM's own `update_spend` — the very function its
scheduler calls. We do not write the row ourselves and we do not reimplement metering;
we make the vendor's own flush happen at the one moment it forgot to.

The wrapper is installed at import time and the module is loaded because `config.yaml`
names it in `litellm_settings.callbacks`, which is resolved during proxy startup — before
any shutdown can occur. `handler` is a no-op logger: the callback list is the only
supported hook for getting our code imported into the proxy, and this module's work is
done by then.

Patching a vendor global is a cost, and the alternative was worse. A FastAPI
`on_event("shutdown")` handler cannot be used: LiteLLM constructs its app with a custom
`lifespan`, and Starlette ignores registered shutdown handlers when a lifespan context is
supplied, so such a handler would look correct and never run. Lowering
`proxy_batch_write_at` only narrows the window; it cannot close it. LiteLLM is MIT, so
there is no licensing bar here of the kind that forbids patching Grafana.

WHAT REMAINS BROKEN, STATED PLAINLY

SIGKILL, an OOM kill, and a hard node failure still lose whatever is in the list — measured,
same experiment with `docker kill -s KILL`, same result. Nothing short of an inline write
survives those, and an inline write means reimplementing the metering path we deliberately
do not own. What this closes is the frequent, ordinary, operator-caused case: a deploy.
That remaining gap is recorded as open in `docs/design/dogfood-findings.md` finding 39
rather than asserted as a test, because a test that requires the loss to persist would have
to hard-kill the gateway on every suite run to prove a defect nobody wants to keep.

`tests/test_spend_survives_restart.py` is the guard. It fails before this module exists,
with `3 of 3 requests were served with HTTP 200 and are not on the bill`.
"""

from litellm.integrations.custom_logger import CustomLogger


def _flush_before_disconnect() -> bool:
    """Wrap `proxy_shutdown_event` so the pending spend batch is committed first.

    Returns True if the wrapper was installed (or already was). Kept as a function with a
    return value so a test can assert the installation happened rather than trusting that
    importing the module had an effect.
    """
    import litellm.proxy.proxy_server as ps
    from litellm._logging import verbose_proxy_logger
    from litellm.proxy.utils import update_spend

    original = ps.proxy_shutdown_event
    if getattr(original, "_eai_flushes_spend", False):
        return True

    async def flush_then_shutdown():
        # A failure here must not block shutdown — but it must be loud, because a silent
        # failure to flush is the exact defect this module exists to remove.
        try:
            if ps.prisma_client is not None:
                await update_spend(
                    prisma_client=ps.prisma_client,
                    db_writer_client=ps.db_writer_client,
                    proxy_logging_obj=ps.proxy_logging_obj,
                )
        except Exception as e:  # pragma: no cover - shutdown path
            verbose_proxy_logger.error(
                "flush_spend_on_shutdown: pending spend rows were NOT committed: %s", e
            )
        await original()

    flush_then_shutdown._eai_flushes_spend = True  # type: ignore[attr-defined]
    ps.proxy_shutdown_event = flush_then_shutdown
    return True


_flush_before_disconnect()


class FlushSpendOnShutdown(CustomLogger):
    """Deliberately does nothing per request. The import above is the whole mechanism."""


handler = FlushSpendOnShutdown()
