"""The LiteLLM -> freerouter cutover mirror, and the reconcile that proves it.

Phase 3 of the cutover (enterpriseaiframework-1f8). Before GATEWAY_PROVIDER can flip, every
(user, surface) that has a LiteLLM virtual key today must already have a freerouter
sub-account carrying the SAME alias and the same budget — so the flip is a config change and
not a re-provisioning event. Nobody re-logs in, nobody loses access, and if the flip has to be
undone the LiteLLM key set is still sitting there untouched.

Three properties hold this together, and each one is a decision rather than an accident:

  * **The mirror is purely additive.** It writes `freerouter_mirror` rows and NOTHING else.
    virtual_key — whose `gateway_token_hash` is the live LiteLLM handle that `/admin/budget`
    and the spend join both use — is never touched. Running the mirror on a live deployment
    changes no user's experience by one byte.

  * **It provisions through `provisioning.backend()`, never straight at freerouter.** The
    selector is the one place that knows which gateway is live, and going around it is how a
    second, divergent mint path gets born. `mirror()` therefore REFUSES to run unless the
    selector actually resolves to freerouter, instead of quietly importing app.freerouter and
    doing it anyway.

  * **The reconcile reports what it could and could not verify, separately.** freerouter's
    subtree rollup is built from recorded GENERATION EVENTS, so a sub-account that has never
    spent does not appear in it (proven against a running binary: mint, then read the rollup,
    and get an empty list). A reconcile that treated the rollup as the account census would
    call every unspent mirror row "missing"; one that ignored the gap would report a green
    0/0 it had not earned. So `reconcile()` splits the answer: rows freerouter CONFIRMED, and
    rows freerouter cannot yet see. Both are counted, neither is disguised as the other.

Budget mirroring is lossy in exactly one direction and the loss is reported, not absorbed:
freerouter's cap is whole-USD and ceil()-rounded, so a $0.50 LiteLLM budget mirrors to $1.
That can only ever be LOOSER than the original, never tighter, so it cannot cost a user access
— but it is a real difference, so `budget_rounded` counts it apart from `budget_mismatch`,
which means freerouter applied something other than what we asked for.
"""

from __future__ import annotations

import argparse
import asyncio
import json

from . import chat_identity, db, freerouter, gateway, provisioning


# ---------------------------------------------------------------- the alias -> account map


async def recorded_account_ids() -> dict[str, str]:
    """alias -> freerouter sub-account id, from our own durable records.

    Installed into app/freerouter as its `alias_resolver` by `install_alias_resolver` so that
    revoke and alias listing stop depending on a sub-account having already spent money.

    `freerouter_mirror` (written only by `mirror()`, for keys carried over from a live LiteLLM
    key before the flip) is the general case. One alias gets a second source on top of it: the
    chat surface's own shared key (chat_identity.SHARED_SURFACE_PRINCIPAL, alias
    "chat-surface::chat", enterpriseaiframework-e6b). That key is not a person's — LibreChat
    holds it, not a user — so `issuance.issue` mints and rotates it DIRECTLY against
    `provisioning.backend()` rather than through `mirror()`, and a key that has never spent
    AND was never mirrored resolves to nothing through either of `_account_ids_by_alias`'s
    other sources: not the rollup (no spend yet), not `freerouter_mirror` (never mirrored).
    Proven against a running freerouter binary before this fix — `delete_by_aliases(...,
    missing_ok=True)` reported success having deleted nothing, and a rotate minted a SECOND
    live sub-account while the pre-rotation one kept authenticating
    (control-plane/tests/test_freerouter_mirror.py
    ::test_rotating_the_shared_chat_key_remaps_it_in_place_without_touching_the_idp).

    `virtual_key.gateway_token_hash` IS the sub-account id for a freerouter-native mint (see
    `freerouter.generate_key`'s `token` field) — but ONLY for that one alias is it safe to read
    it that way here: for every other alias the column may still hold a LiteLLM token hash
    (pre-flip, or simply never mirrored), and treating that as a freerouter account id would
    turn a clean "nothing to revoke" into a DELETE against a nonexistent sub-account. Scoping
    the fallback to the one alias this item owns keeps every other alias's resolution exactly
    as it was.

    It does NOT swallow a missing table. If either record cannot be read, the honest outcome
    is a loud failure — falling back to the rollup would silently restore the exact behaviour
    this exists to remove, and it would do so on the revoke path.
    """
    shared_alias = gateway.surface_alias(chat_identity.SHARED_SURFACE_PRINCIPAL, "chat")
    pool = await db.pool()
    async with pool.acquire() as conn:
        shared_row = await conn.fetchrow(
            "SELECT gateway_token_hash FROM virtual_key "
            "WHERE key_alias = $1 AND status = 'active' AND gateway_token_hash IS NOT NULL",
            shared_alias,
        )
        mirror_rows = await conn.fetch("SELECT key_alias, account_id FROM freerouter_mirror")
    known: dict[str, str] = {}
    if shared_row is not None:
        known[shared_alias] = shared_row["gateway_token_hash"]
    known.update({r["key_alias"]: r["account_id"] for r in mirror_rows})
    return known


def install_alias_resolver() -> None:
    """Point app/freerouter's alias resolution at our own mirror record.

    Called from main.lifespan AFTER db.init(), and by this module's CLI. Explicit rather
    than an import side effect: it needs the schema to be applied first, and a wiring step
    that only happens because somebody imported a module is a wiring step nobody can find.
    """
    freerouter.alias_resolver = recorded_account_ids


# ---------------------------------------------------------------- the two sides of the diff


SOURCE_SQL = """
    SELECT p.username, k.surface, k.key_alias, k.max_budget
      FROM virtual_key k
      JOIN principal p ON p.id = k.principal_id
     WHERE k.status = 'active'
     ORDER BY k.key_alias
"""


async def source_keys(conn) -> list[dict]:
    """Every LIVE LiteLLM (user, surface) key, as the gateway database holds it.

    `status = 'active'` is the filter that matters: a revoked row is a key the gateway no
    longer has, and mirroring it would hand a disabled user a working freerouter key — the
    exact inversion of what the revoke was for.
    """
    rows = await conn.fetch(SOURCE_SQL)
    return [
        {
            "username": r["username"],
            "surface": r["surface"],
            "key_alias": r["key_alias"],
            "max_budget": (
                float(r["max_budget"]) if r["max_budget"] is not None else None
            ),
        }
        for r in rows
    ]


async def mirrored_keys(conn) -> dict[str, dict]:
    """alias -> the mirror row, keyed the same way both sides of the diff are keyed."""
    rows = await conn.fetch(
        "SELECT key_alias, account_id, key_hash, source_max_budget, limit_usd, mirrored_at "
        "FROM freerouter_mirror ORDER BY key_alias"
    )
    return {
        r["key_alias"]: {
            "key_alias": r["key_alias"],
            "account_id": r["account_id"],
            "key_hash": r["key_hash"],
            "source_max_budget": (
                float(r["source_max_budget"])
                if r["source_max_budget"] is not None
                else None
            ),
            "limit_usd": r["limit_usd"],
            "mirrored_at": r["mirrored_at"],
        }
        for r in rows
    }


# ---------------------------------------------------------------- mirroring


class BackendNotFreerouter(RuntimeError):
    """`mirror()` was asked to provision while the selector points at LiteLLM."""


async def record_account(
    conn,
    *,
    key_alias: str,
    account_id: str,
    key_hash: str | None,
    source_max_budget: float | None,
    limit_usd: int | None,
) -> None:
    """Upsert the mirror row that says WHICH sub-account currently wears this alias.

    One statement, used by the initial mirror run and by `/admin/budget`'s rotation, because
    that row is not bookkeeping: `freerouter.alias_resolver` reads it to decide which account
    a revoke addresses. Two copies of this write would be two answers to that question.
    """
    await conn.execute(
        """
        INSERT INTO freerouter_mirror
            (key_alias, account_id, key_hash, source_max_budget, limit_usd)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (key_alias) DO UPDATE
            SET account_id = EXCLUDED.account_id,
                key_hash = EXCLUDED.key_hash,
                source_max_budget = EXCLUDED.source_max_budget,
                limit_usd = EXCLUDED.limit_usd,
                mirrored_at = now()
        """,
        key_alias,
        account_id,
        key_hash,
        source_max_budget,
        limit_usd,
    )


async def mirror(*, dry_run: bool = False) -> dict:
    """Provision a freerouter sub-account for every live LiteLLM key that lacks one.

    Idempotent by alias: a second run mints nothing, because the mirror row is what says a
    given (user, surface) is already carried across. That matters more than it looks — this
    runs against production, and a mint that ran twice would leave a stranded sub-account
    holding the alias, so the operator bill would show two rows for one user's surface.

    Returns a summary; `details` names every alias it actually minted.
    """
    if provisioning.backend() is not freerouter:
        raise BackendNotFreerouter(
            "mirror() provisions through provisioning.backend(), and the selector currently "
            f"resolves to {provisioning.backend().__name__} — set GATEWAY_PROVIDER=freerouter "
            "for the mirror run. (The FLIP is separate: mirroring does not change which "
            "gateway serves traffic.)"
        )

    pool = await db.pool()
    async with pool.acquire() as conn:
        source = await source_keys(conn)
        already = await mirrored_keys(conn)

    todo = [row for row in source if row["key_alias"] not in already]
    details: list[dict] = []
    for row in todo:
        if dry_run:
            details.append({"key_alias": row["key_alias"], "action": "would_mint"})
            continue
        created = await provisioning.generate_key(
            username=row["username"],
            surface=row["surface"],
            # The freerouter path attributes by the sub-account label, but the argument is
            # part of the shared provisioning contract and is passed through unchanged.
            idp_user_id=row["username"],
            max_budget=row["max_budget"],
        )
        async with pool.acquire() as conn:
            await record_account(
                conn,
                key_alias=row["key_alias"],
                account_id=created["token"],
                key_hash=created.get("key_hash"),
                source_max_budget=row["max_budget"],
                limit_usd=created.get("limit_usd"),
            )
        await db.audit(
            "system",
            "freerouter.mirror",
            row["username"],
            surface=row["surface"],
            key_alias=row["key_alias"],
            account_id=created["token"],
            limit_usd=created.get("limit_usd"),
            blocked=created.get("blocked", False),
        )
        details.append(
            {
                "key_alias": row["key_alias"],
                # "minted_blocked": a 0-or-negative LiteLLM budget mirrored to a REAL
                # sub-account whose key freerouter.generate_key disabled before returning it
                # (enterpriseaiframework-9ef) — never the unlimited key omitting `limit` used
                # to produce. The row is still written like any other mirror: the account_id
                # is real and addressable, only the key on it can never authenticate.
                "action": "minted_blocked" if created.get("blocked") else "minted",
                "account_id": created["token"],
                "limit_usd": created.get("limit_usd"),
            }
        )
        # The minted bearer is the USER's key and is deliberately dropped here. The operator
        # hands it out by rotating through issuance.issue at cutover, or the surface collects
        # it then; the mirror does not become a place worth stealing.

    return {
        "source_keys": len(source),
        "already_mirrored": len(already),
        "minted": 0 if dry_run else len(todo),
        "would_mint": len(todo) if dry_run else 0,
        "details": details,
    }


# ---------------------------------------------------------------- reconciling


async def reconcile(*, verify_litellm: bool = False) -> dict:
    """Diff the live LiteLLM key set against the freerouter sub-accounts, and report.

    `missing` and `budget_mismatch` are the two numbers the cutover gate reads; both must be
    zero before GATEWAY_PROVIDER flips. Everything else is there so a zero is believable:

      * `missing`            — a live LiteLLM key with no freerouter sub-account. The blocker.
      * `budget_mismatch`    — freerouter applied a cap other than the one the LiteLLM budget
                               asks for. Compared against `limit_usd`, which is FREEROUTER's
                               reported answer, not our request echoed back.
      * `budget_rounded`     — mirrored faithfully but rounded up to whole USD by freerouter's
                               cap granularity. Not a blocker (never tighter than the
                               original), but never silently swallowed either.
      * `orphans`            — a freerouter sub-account with no live LiteLLM key behind it,
                               e.g. a user revoked after the mirror ran. It would keep working
                               after the flip, so it is a real finding.
      * `confirmed`          — mirror rows freerouter's own rollup corroborates, by account id.
      * `unconfirmed`        — mirror rows freerouter cannot corroborate because the
                               sub-account has never spent and the rollup is built from spend
                               events. NOT counted as missing, and NOT counted as verified.
      * `account_id_conflict`— the rollup shows accounts for an alias we recorded and NONE of
                               them is the one we recorded. The bill cannot say which account
                               is ours, so it is called out on its own and it fails the gate.
      * `retired_accounts`   — the rollup shows an EXTRA account beside the recorded one. A
                               budget change rotates the sub-account (freerouter cannot re-cap
                               a key) and the retired account keeps its bill rows, so this is
                               the ordinary tail of a rotation and does not fail the gate. It
                               is still reported: if a retired account's spend is growing, the
                               revoke that follows the rotation did not land and the user is
                               holding two live keys at two different caps.
    """
    pool = await db.pool()
    async with pool.acquire() as conn:
        source = await source_keys(conn)
        mirrored = await mirrored_keys(conn)

    # Every account the rollup shows per alias, not one of them: a budget change rotates the
    # sub-account and freerouter keeps the retired account's bill rows, so a rotated alias
    # legitimately has two. Collapsing them would report the live, recorded account as an
    # `account_id_conflict` roughly half the time, purely by dict order.
    rollup_all = await freerouter.rollup_accounts_by_alias()

    source_by_alias = {r["key_alias"]: r for r in source}
    missing = sorted(set(source_by_alias) - set(mirrored))
    orphans = sorted(set(mirrored) - set(source_by_alias))

    budget_mismatch: list[dict] = []
    budget_rounded: list[dict] = []
    for alias, src in source_by_alias.items():
        row = mirrored.get(alias)
        if row is None:
            continue  # already counted as missing
        expected = freerouter.budget_to_monthly_usd(src["max_budget"])
        if row["limit_usd"] != expected:
            budget_mismatch.append(
                {
                    "key_alias": alias,
                    "litellm_max_budget": src["max_budget"],
                    "expected_limit_usd": expected,
                    "freerouter_limit_usd": row["limit_usd"],
                }
            )
        elif expected is not None and float(expected) != src["max_budget"]:
            budget_rounded.append(
                {
                    "key_alias": alias,
                    "litellm_max_budget": src["max_budget"],
                    "freerouter_limit_usd": row["limit_usd"],
                }
            )

    confirmed: list[str] = []
    unconfirmed: list[str] = []
    account_id_conflict: list[dict] = []
    retired_still_spending: list[dict] = []
    for alias, row in mirrored.items():
        seen = rollup_all.get(alias, [])
        if not seen:
            unconfirmed.append(alias)
        elif row["account_id"] not in seen:
            # NONE of the accounts freerouter reports under this alias is the one we
            # recorded. That is two accounts wearing one label with the bill unable to say
            # which is ours, and it is a different fault from a rotation's harmless tail.
            account_id_conflict.append(
                {"key_alias": alias, "recorded": row["account_id"], "rollup": sorted(seen)}
            )
        else:
            confirmed.append(alias)
        stale = [a for a in seen if a != row["account_id"]]
        if stale:
            # A rotation's retired account, still carrying its (frozen) bill rows: expected
            # and harmless. It is reported anyway because a retired account whose spend is
            # STILL GROWING means the revoke that should have followed the rotation never
            # landed and the user is holding two live keys at two different caps.
            retired_still_spending.append(
                {"key_alias": alias, "live": row["account_id"], "retired": sorted(stale)}
            )

    result = {
        "source_keys": len(source),
        "mirrored_keys": len(mirrored),
        "missing": missing,
        "orphans": orphans,
        "budget_mismatch": budget_mismatch,
        "budget_rounded": budget_rounded,
        "confirmed": sorted(confirmed),
        "unconfirmed": sorted(unconfirmed),
        "account_id_conflict": account_id_conflict,
        "retired_accounts": retired_still_spending,
        "ok": not missing and not budget_mismatch and not account_id_conflict,
    }

    if verify_litellm:
        # The gateway database is a MIRROR of LiteLLM, so on its own it can only prove the
        # mirror agrees with our record of LiteLLM. This asks LiteLLM itself, and is the
        # check that catches a key someone minted straight at the gateway.
        live = set(await gateway.list_aliases())
        db_aliases = set(source_by_alias)
        result["litellm_only"] = sorted(live - db_aliases)
        result["db_only"] = sorted(db_aliases - live)
        result["ok"] = result["ok"] and not result["litellm_only"]

    return result


# ---------------------------------------------------------------- CLI
#
# `python -m app.mirror --reconcile` inside the control-plane container. Exit status IS the
# gate: 0 only when the cutover is safe, so it drops straight into a deploy script without
# anybody having to parse the JSON to find out whether it passed.


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m app.mirror",
        description="Mirror LiteLLM virtual keys into freerouter and reconcile the two.",
    )
    p.add_argument("--mirror", action="store_true", help="provision the missing sub-accounts")
    p.add_argument(
        "--dry-run", action="store_true", help="with --mirror: report, provision nothing"
    )
    p.add_argument("--reconcile", action="store_true", help="diff and report (the gate)")
    p.add_argument(
        "--verify-litellm",
        action="store_true",
        help="with --reconcile: also diff the gateway database against LiteLLM itself",
    )
    return p


async def _amain(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if not args.mirror and not args.reconcile:
        _build_parser().print_help()
        return 2

    install_alias_resolver()
    out: dict = {}
    if args.mirror:
        out["mirror"] = await mirror(dry_run=args.dry_run)
    if args.reconcile:
        out["reconcile"] = await reconcile(verify_litellm=args.verify_litellm)

    print(json.dumps(out, indent=2, default=str))
    if args.reconcile and not out["reconcile"]["ok"]:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_amain(argv))


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess by the tests
    raise SystemExit(main())
