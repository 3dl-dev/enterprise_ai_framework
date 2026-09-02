"""freerouter provisioning — the inference-spoke analogue of app/gateway.py.

The control plane provisions keys against the bundled **freerouter** spoke instead of
LiteLLM. It is signature-compatible with app/gateway.py so the same callers (agents.py,
the portal, the sync loop) drive either backend, selected by GATEWAY_PROVIDER. Design:
docs/design/records/freerouter-reference-router.md (C1/C3), item enterpriseaiframework-757.

The one model difference from LiteLLM, and how it is bridged here:

  LiteLLM has a single master key that mints aliased keys, each carrying its own budget.
  freerouter has no admin scope at all: `requireTenant` resolves a bearer to a *tenant*, and
  every write is scoped to the CALLING tenant's own subtree. So the control plane is itself
  ONE freerouter tenant — provisioned once at bootstrap via `POST /api/v1/signup`, its
  one-time bearer stored as FREEROUTER_MASTER_KEY (the GATEWAY_MASTER_KEY analogue) — and
  every `<user>::<surface>` is a nested SUB-ACCOUNT under it (item enterpriseaiframework-1da),
  so the operator bill attributes per (user,surface) by account id.

  The BUDGET then lands one level further down, and that is not a stylistic choice: a cap is
  a per-KEY property set at mint, and `POST /api/v1/keys` is scoped to the caller. The parent
  therefore cannot cap its child's key, so `generate_key` uses the sub-account's own one-time
  bearer, once, in-process, to mint the capped key it hands to the surface.

  That same fact makes CHANGING a budget a rotation rather than a patch — see `update_budget`.
  A rotated alias is therefore worn by more than one account over time, which is why every
  alias→account read here treats the control plane's own record as authoritative and the
  spend-derived rollup as corroboration.

Confirmed live against a running freerouter binary (signup=open), including the two facts
this module's shape depends on — the parent cannot read or set a child's cap, and the rollup
carries only accounts that have SPENT:
  POST   /api/v1/signup      {display_name} -> {data:{account_id, parent_account_id, api_key}}
  POST   /api/v1/subaccounts {name}         -> {data:{account_id, api_key, name}}
  DELETE /api/v1/subaccounts/{account_id}   -> revokes the child's keys, keeps its bill rows
  POST   /api/v1/keys        {name, limit}  -> {data:{hash, name, label, limit, ...}, key}
  GET    /api/v1/keys                       -> the CALLER's own keys only
  PATCH  /api/v1/keys/{hash} {name,disabled} -- carries NO limit: a cap is fixed at mint
  GET    /api/v1/usage/rollup               -> subtree rows, SPENT accounts only
"""

from __future__ import annotations

import math
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx

# Alias grammar is provider-agnostic — reuse it so both backends attribute identically.
from .gateway import parse_alias, surface_alias


def base_url() -> str:
    return os.environ.get("FREEROUTER_URL", "http://freerouter:8080").rstrip("/")


def _headers() -> dict:
    """The control plane's own tenant bearer — freerouter's GATEWAY_MASTER_KEY analogue.

    Bootstrapped once (a single POST /api/v1/signup at deploy time) and injected as a
    secret, exactly as GATEWAY_MASTER_KEY is. All sub-keys are minted under this tenant.
    """
    return {"Authorization": f"Bearer {os.environ['FREEROUTER_MASTER_KEY']}"}


async def _request(method: str, path: str, **kwargs) -> httpx.Response:
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.request(
            method, f"{base_url()}{path}", headers=_headers(), **kwargs
        )
        resp.raise_for_status()
        return resp


def budget_to_monthly_usd(max_budget: float | None) -> int | None:
    """The LiteLLM budget as freerouter's per-key cap, or None for unlimited.

    freerouter's only spend cap is a per-KEY MONTHLY budget in WHOLE USD, ceil()-rounded and
    with 0 meaning unlimited (internal/core/keys.go `limitToMonthlyUSD`, verified against a
    running binary). LiteLLM's `max_budget` is an unrounded float. The conversion is therefore
    LOSSY IN ONE DIRECTION ONLY — 0.5 becomes 1, never 0 — so a mirrored cap is never TIGHTER
    than the LiteLLM one and no user loses access to spend they already had. The reconcile
    reports the rounding separately from a real mismatch so the loss is visible rather than
    absorbed here.
    """
    if max_budget is None or max_budget <= 0:
        return None
    return math.ceil(max_budget)


async def generate_key(
    *, username: str, surface: str, idp_user_id: str, max_budget: float | None
) -> dict:
    """Mint a nested sub-ACCOUNT bound to one user and one surface, plus its capped key.

    Each (user,surface) is its OWN sub-account under the control-plane tenant, not a sub-key,
    so the operator bill (freerouter-573 rollup) attributes per (user,surface) by AccountID.
    POST /api/v1/subaccounts {name:"<user>::<surface>"} → {data:{account_id, api_key, name}}.

    Then the BUDGET, which the sub-account alone cannot carry. freerouter has no per-account
    cap (freerouter-171/ca9) — its one spend cap is a per-KEY monthly limit set at mint,
    `POST /api/v1/keys {name, limit}`. That route is scoped to the CALLING tenant, so the only
    principal that can put a cap on this user's key is the sub-account ITSELF. So the mint is
    two calls: create the sub-account, then use its one-time bearer ONCE, in-process, to mint
    the named, capped key that is actually handed to the surface. The sub-account's root bearer
    is dropped on the floor and never persisted — it is not returned, not stored, and not
    recoverable, so the control plane still holds no credential for anybody.

    Returns the caller contract gateway.generate_key satisfies — `token` = the durable handle
    the control plane stores (the ACCOUNT_ID, which is what revoke addresses), `key` = the
    one-time bearer handed to the surface — plus, additively, `key_hash` and `limit_usd` as
    FREEROUTER REPORTED them. limit_usd is the gateway's own answer about the cap it applied,
    which is what makes a later budget reconcile a comparison against freerouter rather than
    against our own request. `idp_user_id` rides for gateway.py parity; attribution is by the
    sub-account label.

    A `max_budget` of 0 or less is BLOCKED, never minted unlimited (enterpriseaiframework-9ef,
    the ADMITTED hole `update_budget`'s own pre-check (enterpriseaiframework-257) does not
    cover: `mirror.mirror()` and `issuance.issue()` both call this function directly, with no
    guard of their own, for every (user, surface) whose LiteLLM budget happens to be zero).
    `budget_to_monthly_usd` returns None for BOTH "no cap was ever set" (max_budget is None)
    and "the cap is zero/negative" (max_budget <= 0) — freerouter's `limit<=0` means UNLIMITED
    (internal/core/keys.go `limitToMonthlyUSD`), so omitting `limit` for a zero budget would
    mint the MOST permissive key in the system for the user least entitled to spend at all.

    The decision, DOCUMENTED here rather than left implicit: MINT, THEN DISABLE — not
    refuse-to-mint. `POST /api/v1/subaccounts` and `POST /api/v1/keys` proceed exactly as
    for any other budget, then, only for the blocked case, one extra call —
    `PATCH /api/v1/keys/{hash} {"disabled": true}` — using the same one-time sub-account
    bearer before it is dropped. freerouter's PATCH-disable is a ONE-WAY REVOKE through the
    metering library (internal/core/keys.go: `Disabled:true` calls `RevokeSubKey`; there is no
    un-disable), so the key handed back can authenticate never — not "until someone notices",
    not "until it spends $1" — proven against a running binary in
    test_freerouter_mirror.py::test_generate_key_blocks_rather_than_mints_unlimited_for_a_zero_budget.
    Refuse-to-mint (skip the row, mark it blocked in `freerouter_mirror`) was the other option
    named in the item and was rejected here: `freerouter_mirror.account_id` is NOT NULL by
    design (every mirror row names a real, addressable sub-account — see app/db.py), so
    "recorded but blocked" would need a schema change; `issuance.issue()` also has no path to
    hand a caller "no key" without turning an ordinary zero-budget rotation into a 500 for a
    user who did nothing wrong. Mint-then-disable keeps ONE code path, no schema change, and a
    freerouter account_id that always resolves — the key on it simply never authenticates.
    """
    alias = surface_alias(username, surface)
    resp = await _request("POST", "/api/v1/subaccounts", json={"name": alias})
    data = resp.json()["data"]

    blocked = max_budget is not None and max_budget <= 0
    limit = None if blocked else budget_to_monthly_usd(max_budget)
    body: dict = {"name": alias}
    if limit is not None:
        body["limit"] = limit
    # The sub-account's own bearer, used here and nowhere else. A local client rather than
    # `_request`, which carries the control-plane tenant's header and therefore would mint the
    # key under the CONTROL PLANE's account — attributing every user's spend to the operator.
    async with httpx.AsyncClient(timeout=30.0) as client:
        minted = await client.post(
            f"{base_url()}/api/v1/keys",
            headers={"Authorization": f"Bearer {data['api_key']}"},
            json=body,
        )
        minted.raise_for_status()
        key = minted.json()
        if blocked:
            # See the docstring: a zero/negative budget mints, then is revoked in-place
            # before it is ever handed back, so it is dead on arrival rather than unlimited.
            disable = await client.patch(
                f"{base_url()}/api/v1/keys/{key['data']['hash']}",
                headers={"Authorization": f"Bearer {data['api_key']}"},
                json={"disabled": True},
            )
            disable.raise_for_status()
    return {
        "token": data["account_id"],
        "key": key["key"],
        "key_hash": key["data"]["hash"],
        # freerouter renders an unlimited cap as null, not 0.
        "limit_usd": (int(key["data"]["limit"]) if key["data"].get("limit") else None),
        "blocked": blocked,
        "data": data,
    }


# The control plane's OWN record of alias → sub-account id, installed by app/mirror.py at
# import. It exists because of a measured hole, not a hypothetical one:
#
#   GET /api/v1/usage/rollup is built ONLY from recorded generation events
#   (freerouter metering/rollup.go: it aggregates the usage ledger, then filters to the
#   caller's subtree). A sub-account that has never spent produces no event, so it does not
#   appear in the rollup AT ALL — confirmed against a running freerouter: mint a sub-account,
#   read the rollup, get `{"data":[]}`.
#
# Every read below was derived from that rollup, so before this hook a freshly-minted key was
# invisible to the control plane: `list_aliases` said the user had no key, and
# `delete_by_aliases(..., missing_ok=True)` — the ROTATE path in issuance.py and the
# disabled-in-IdP revoke in main.py — reported success while revoking nothing, leaving a live
# key behind for a user identity had just switched off. Resolving the alias from our own
# durable record closes that: the DB knows every sub-account we ever minted, spent or not.
#
# A callable rather than an import so app/freerouter.py keeps no database dependency of its
# own (it is also driven from scripts with no pool) — unset, behaviour is exactly the old
# rollup-only read.
alias_resolver: Callable[[], Awaitable[dict[str, str]]] | None = None


async def rollup_accounts_by_alias() -> dict[str, list[str]]:
    """alias "<user>::<surface>" → EVERY sub-account id the rollup shows wearing it (573).

    A list and not a single id, because one alias legitimately outlives one account. A budget
    change on this backend rotates the sub-account (`update_budget`), and freerouter's revoke
    keeps the retired account's BILL ROWS — so the rollup goes on reporting the old account
    under the same label forever. Collapsing that to one id silently picks a winner by dict
    order; the callers that need one id say which one they mean.
    """
    resp = await _request("GET", "/api/v1/usage/rollup")
    out: dict[str, list[str]] = {}
    for a in resp.json().get("data", []):
        if not isinstance(a, dict) or not a.get("name") or not a.get("account_id"):
            continue
        out.setdefault(a["name"], []).append(a["account_id"])
    return out


async def rollup_account_ids_by_alias() -> dict[str, str]:
    """alias → ONE sub-account id from the operator subtree rollup (573).

    freerouter's side of the mirror, and SPENT-ONLY by construction — see `alias_resolver`.
    Kept for readers that only need to know whether freerouter can see the alias at all; when
    an alias has been rotated the rollup holds several and this reports the last one, so
    anything that must be RIGHT about which account is live reads `_account_ids_by_alias`
    (our own record) or `rollup_accounts_by_alias` (all of them).
    """
    return {alias: ids[-1] for alias, ids in (await rollup_accounts_by_alias()).items()}


async def _account_ids_by_alias() -> dict[str, str]:
    """alias → the sub-account id that is LIVE for it: freerouter's rollup, then our record.

    Our record goes SECOND and therefore wins. That ordering is the whole correctness of the
    revoke path once budgets can change: `update_budget` rotates the sub-account (freerouter
    has no way to re-cap an existing key), the retired account keeps its bill rows and so
    keeps appearing in the rollup under the SAME alias, and letting the rollup win would
    resolve the alias to the DEAD account — `delete_by_aliases` would then report success
    having revoked nothing, leaving the user's live key working. The mirror record is the
    control plane's statement of which account currently wears the alias; the rollup is a
    spend history that cannot distinguish the current holder from a retired one. The rollup
    still WIDENS the map, so an account minted outside our record is not invisible.
    """
    known: dict[str, str] = await rollup_account_ids_by_alias()
    if alias_resolver is not None:
        known.update(await alias_resolver())
    return known


async def delete_by_aliases(aliases: list[str], *, missing_ok: bool = False) -> dict:
    """Hard-revoke each (user,surface) by alias, parent-scoped.

    DELETE /api/v1/subaccounts/{account_id} disables every key under the sub-account so it
    can't authenticate — ledger/bill row intact, and the control plane never holds the user's
    key (posture preserved). missing_ok mirrors gateway: nothing to revoke is a valid outcome.
    """
    if not aliases:
        return {"deleted_keys": []}
    by_alias = await _account_ids_by_alias()
    wanted = [(a, by_alias[a]) for a in aliases if a in by_alias]
    if not wanted:
        if missing_ok:
            return {"deleted_keys": []}
        raise KeyError(f"no freerouter sub-account matches aliases {aliases}")
    deleted: list[str] = []
    for alias, account_id in wanted:
        await _request("DELETE", f"/api/v1/subaccounts/{account_id}")
        deleted.append(alias)
    return {"deleted_keys": deleted}


async def revoke_token(token: str, *, missing_ok: bool = True) -> dict:
    """Hard-revoke ONE sub-account by its id, without going through the alias.

    The alias-addressed revoke (`delete_by_aliases`) cannot express "retire the account this
    alias USED to point at", which is exactly what finishing a budget rotation needs: by then
    two accounts have worn the alias and the live one is the new one. Addressing the id
    directly is unambiguous. missing_ok, as everywhere else here, means an account that is
    already gone is a valid outcome and not an error.
    """
    try:
        await _request("DELETE", f"/api/v1/subaccounts/{token}")
    except httpx.HTTPStatusError as exc:
        if missing_ok and exc.response.status_code == 404:
            return {"revoked": []}
        raise
    return {"revoked": [token]}


class BudgetNotExpressible(ValueError):
    """The requested cap has no faithful freerouter representation, so it is refused."""


async def update_budget(token_hash: str, max_budget: float) -> dict:
    """Change a user's cap by ROTATING their sub-account, because freerouter cannot patch one.

    A cap is a per-KEY property fixed at `POST /api/v1/keys` time: `PATCH /api/v1/keys/{hash}`
    carries only `name` and `disabled` (internal/core/keys.go PatchKeyRequest — verified
    against a running binary), and there is no per-ACCOUNT cap at all (freerouter-171/ca9).
    Nor can the control plane mint a second key at the new cap under the EXISTING sub-account:
    `POST /api/v1/keys` is scoped to the CALLING tenant, and `generate_key` deliberately drops
    the sub-account's one-time bearer on the floor rather than becoming a credential store. So
    the only way to move a cap is a new sub-account carrying the same `<user>::<surface>`
    alias, with its key minted at the new limit.

    This used to raise NotImplementedError, which made `/admin/budget` a 500 on the freerouter
    backend — the flip blocker this replaces (enterpriseaiframework-257).

    THE MINT ONLY. It does not retire the old account, and the ordering is the point: the
    caller has to write the new handle into the ledger before anything revokes the old one, or
    a crash between the two takes the user's working key away and loses its replacement. See
    main.set_budget, which persists and then calls `revoke_token(result["retire_token"])`.
    Until that revoke lands the user keeps their existing key — they never lose access
    mid-change — at the OLD cap, which is why the retire is a step and not an option.

    Returns `generate_key`'s dict plus the three fields the continuity needs: `rotated` (the
    marker main.set_budget branches on — the LiteLLM backend patches in place and never sets
    it), `key_alias`, and `retire_token`.
    """
    if max_budget is not None and max_budget <= 0:
        # freerouter reads limit<=0 as UNLIMITED (limitToMonthlyUSD), so mirroring a zero cap
        # would hand the user an uncapped key — the exact inversion of what a zero budget is
        # for. There is no "spend nothing" cap to mint, so refuse and name the operation that
        # does express it, rather than quietly minting the most permissive key in the system.
        raise BudgetNotExpressible(
            f"freerouter cannot express a cap of {max_budget}: a limit of 0 or less means "
            "UNLIMITED (internal/core/keys.go limitToMonthlyUSD), so this would remove the "
            "cap instead of applying it. To stop a user spending, revoke the key."
        )

    known = await _account_ids_by_alias()
    alias = next((a for a, account_id in known.items() if account_id == token_hash), None)
    if alias is None:
        raise KeyError(
            f"no freerouter sub-account is recorded for handle {token_hash!r}; the budget "
            "cannot be rotated onto an account the control plane cannot name"
        )
    parsed = parse_alias(alias)
    if parsed is None:
        raise KeyError(f"freerouter alias {alias!r} does not parse to (user, surface)")
    username, surface = parsed

    created = await generate_key(
        username=username, surface=surface, idp_user_id=username, max_budget=max_budget
    )
    return {
        **created,
        "rotated": True,
        "key_alias": alias,
        "retire_token": token_hash,
        "max_budget": max_budget,
    }


async def list_keys() -> list[dict]:
    """The control plane's sub-accounts as the operator subtree rollup reports them."""
    resp = await _request("GET", "/api/v1/usage/rollup")
    return [a for a in resp.json().get("data", []) if isinstance(a, dict)]


async def token_hashes_by_alias() -> dict[str, str]:
    """alias "<user>::<surface>" → sub-account id (the durable handle the control plane stores
    at mint). Freerouter-side view, used to verify/backfill."""
    return await _account_ids_by_alias()


async def list_aliases(prefix: str | None = None) -> list[str]:
    """Sub-account aliases the operator subtree currently shows. Used to verify revocation."""
    names = list((await _account_ids_by_alias()).keys())
    return [a for a in names if not prefix or a.startswith(prefix)]


async def health() -> bool:
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{base_url()}/healthz")
            return r.status_code == 200
    except Exception:
        return False


async def _signup_operator_tenant() -> str:
    """Provision the control plane's own freerouter tenant, returning its one-time bearer.

    freerouter has no admin key: the control plane is itself a tenant under op-root, created
    by a one-time `POST /api/v1/signup` (requires FREEROUTER_SIGNUP=open on the spoke).
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{base_url()}/api/v1/signup",
            json={"display_name": "enterprise-ai-control-plane"},
        )
        resp.raise_for_status()
        return resp.json()["data"]["api_key"]


def keyfile_path() -> str | None:
    p = os.environ.get("FREEROUTER_MASTER_KEY_FILE", "").strip()
    return p or None


async def bootstrap_master_key() -> str:
    """Resolve the control plane's freerouter bearer, provisioning + persisting it once.

    Precedence: an injected FREEROUTER_MASTER_KEY secret wins; else a previously-persisted
    keyfile (FREEROUTER_MASTER_KEY_FILE) is reused; else the tenant is signed up ONCE and the
    one-time bearer is written to the keyfile so it survives restarts — signing up twice would
    strand a second empty tenant. The resolved key is exported into the process env so the
    rest of this module (`_headers`) uses it unchanged.

    Idempotent across restarts as long as either the secret or the keyfile persists; call it
    once at startup when GATEWAY_PROVIDER=freerouter.
    """
    existing = os.environ.get("FREEROUTER_MASTER_KEY", "").strip()
    if existing:
        return existing

    path = keyfile_path()
    if path:
        try:
            stored = Path(path).read_text().strip()
        except FileNotFoundError:
            stored = ""
        if stored:
            os.environ["FREEROUTER_MASTER_KEY"] = stored
            return stored

    bearer = await _signup_operator_tenant()
    if path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(bearer)
        p.chmod(0o600)
    os.environ["FREEROUTER_MASTER_KEY"] = bearer
    return bearer
