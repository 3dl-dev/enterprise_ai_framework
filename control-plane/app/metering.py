"""The one bill.

Scope item 4: a single query returns total spend broken down by user and by surface,
across all three surfaces.

The gateway already meters every request it serves and writes a spend row. We do not
duplicate that ledger — we read it and join it to identity through the key alias, which
is the whole reason the alias carries the surface. Reimplementing metering in the
control plane would put us in the data path for no gain.
"""

import os

import asyncpg

_pool: asyncpg.Pool | None = None


async def pool() -> asyncpg.Pool:
    """Read-only-by-convention pool against the gateway's own database."""
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn=os.environ["GATEWAY_DATABASE_URL"], min_size=1, max_size=5
        )
    return _pool


# LiteLLM hashes the key into LiteLLM_SpendLogs.api_key and keeps the alias on
# LiteLLM_VerificationToken.token. The join is on the hashed token.
#
# That join alone is not enough, and the reason is a defect this row hit for real. The
# join is against a table we DELETE from: revoking a disabled user's keys (scope item 6),
# rotating a key when a surface is reprovisioned, and the exit path's revoke-all all
# remove the LiteLLM_VerificationToken row. Every historical spend row for that key then
# joins to NULL and falls out of the bill as "(unattributed)" — observed on the cluster at
# 88% of all spend after a handful of workspace reprovisions.
#
# The bill going quiet about money that was definitely spent is the worst failure this
# component has, so attribution is taken from the alias LiteLLM stamps onto the spend row
# itself at request time, which nothing later deletes. The join survives as a fallback for
# rows written before that metadata existed.
_ALIAS = """COALESCE(
    NULLIF(s.metadata->>'user_api_key_alias', ''),
    NULLIF(v.key_alias, '')
)"""

_LEDGER_JOIN = """
FROM "LiteLLM_SpendLogs" s
LEFT JOIN "LiteLLM_VerificationToken" v ON v.token = s.api_key
"""


# WHICH KEYS MAY NAME SOMEONE OTHER THAN THEMSELVES
#
# `end_user` is whatever the caller put in the request body's "user" field. For a surface
# that serves many people through ONE key it is the only way to tell them apart, and the
# chat surface is exactly that: LibreChat authenticates the person, then forwards them as
# `user`. For a per-user key it is redundant, because the alias already says who holds it.
#
# Trusting it everywhere meant the caller chose the name on the bill. Demonstrated on this
# cluster with a legitimate `baron::ide` key and a body of {"user":"veracity-probe-xyz"}:
# the spend appeared under `veracity-probe-xyz`. The money could not escape the key's own
# budget — caps bind to the key — but attribution is the product, and attribution was
# forgeable by anybody holding any key.
#
# So end_user is honoured only for keys minted AS shared surfaces, and ignored everywhere
# else in favour of the alias. Defaults to the one shared key we mint (provision-chat-key.sh
# and post-deploy.sh both use this alias); override for a deployment that adds another.
#
# Failing closed is the point: an alias that is absent, deleted, or simply not on this list
# falls through to the alias-derived name, which the holder cannot choose.
SHARED_SURFACE_ALIASES = [
    a.strip() for a in os.environ.get(
        "SHARED_SURFACE_ALIASES", "chat-surface::chat"
    ).split(",") if a.strip()
]

# Only a key on that list gets to speak for someone else.
_TRUSTED_END_USER = f"""CASE
    WHEN {_ALIAS} = ANY($SHARED::text[]) THEN NULLIF(s.end_user, '')
    ELSE NULL
END"""

# What the bill calls a row it cannot put a name to. Money that was definitely spent stays
# on the bill under this label; dropping it would hide it and guessing would be worse.
UNATTRIBUTED = "(unattributed)"


def ledger_attribution_sql(shared_param: str) -> dict[str, str]:
    """The one attribution rule, as SQL fragments, for every rendering of the ledger.

    THERE IS EXACTLY ONE OF THESE ON PURPOSE. The ledger is rendered three ways — the
    aggregate bill below, the portal built on it, and the per-request CSV the exit path
    hands a departing customer — and each rendering that carries its own copy of the join
    is a place the next correction gets forgotten. It was forgotten twice already:
    finding 25 fixed the deleted-key join in the bill and left `export.py` on the old one,
    and finding 34 fixed the naming in the portal and left `/admin/spend` behind. The
    export was the worst of the three, because the exit path revokes every key *before*
    exporting, so the CSV attributed from a table it had just emptied.

    `shared_param` is the positional placeholder ($1, $2, …) that the caller has bound to
    SHARED_SURFACE_ALIASES in the query being built. It is a parameter rather than an
    interpolated list so the shared-surface list can never be injected through.

    Returns the fragments a renderer needs, all of which assume the returned `join`:

      join      the FROM/LEFT JOIN clause, aliasing the ledger `s` and the key table `v`
      alias     the key alias for the row, metadata first (NULL if genuinely unknown)
      surface   the surface encoded in that alias      (NULL if genuinely unknown)
      principal whose spend this is — never NULL, falls back to UNATTRIBUTED
    """
    trusted = _TRUSTED_END_USER.replace("$SHARED", shared_param)
    return {
        "join": _LEDGER_JOIN,
        "alias": _ALIAS,
        "surface": f"NULLIF(split_part({_ALIAS}, '::', 2), '')",
        # Attribution precedence: the end user a SHARED surface forwarded, then the user
        # encoded in the key alias. See SHARED_SURFACE_ALIASES — a per-user key naming
        # somebody else is ignored, because otherwise the caller picks who gets billed.
        "principal": (
            f"COALESCE(\n            {trusted},\n"
            f"            NULLIF(split_part({_ALIAS}, '::', 1), ''),\n"
            f"            '{UNATTRIBUTED}'\n        )"
        ),
    }


# WHY THE BILL COUNTS CACHE HITS SEPARATELY (enterpriseaiframework-d58)
#
# The gateway serves a repeated request out of Valkey without calling the provider, and
# writes a spend row for it: right key, real token counts, spend $0. Every part of that is
# correct — no upstream call was made, so no money was spent, and a non-zero number there
# would be one no invoice will ever confirm.
#
# But a bill that reports "85 requests, 150,574 tokens, $0.00" and says nothing else is
# indistinguishable from a bill that has lost the money, and it was read as exactly that —
# those are the real cluster figures, filed twice as the bill under-reporting. The $0 is
# not the problem; the $0 being unexplained is. So the count travels with it, and an
# operator can see that the free rows were free because they had already been answered.
#
# LiteLLM writes this column as the strings 'True'/'False', and leaves it NULL on paths
# that predate it — hence the case-insensitive compare against 'true' rather than a
# boolean cast, which would error on the NULLs. Same predicate as unpriced_models below,
# deliberately: the two must agree about what a cache hit is.
_CACHE_HIT = "lower(COALESCE(s.cache_hit, '')) = 'true'"


# WHY A FAILED REQUEST STAYS IN `requests` AND IS NAMED SEPARATELY (enterpriseaiframework-e69)
#
# THREE OUTCOMES REACH THIS LEDGER, NOT TWO. The first version of this comment said two,
# and the code below matched the comment rather than the database. Every class was then
# re-measured on the bundle, one fresh key each, and dumped raw:
#
#   outcome                        status   error_class            llm_provider  alias
#   ------------------------------ -------- ---------------------- ------------- -----
#   served                         success  (no error object)      (no error)    yes
#   served from cache              success  (no error object)      (no error)    yes
#   upstream 500                   failure  InternalServerError    openai        yes
#   upstream has no such route *   failure  NotFoundError          openai        yes
#   no attributable principal **   failure  HTTPException     403  ''            NO
#   rate limit the GATEWAY imposed failure  RouterRateLimitError † ''            yes
#                                  failure  HTTPException     429  ''            yes
#   model not on the key's list    failure  ProxyException    401  ''            NO
#   over the key's budget          failure  BudgetExceededError    ''            NO
#   model not in the catalogue     failure  ProxyException    401  ''            yes
#
#   and the four typed columns beside them, which is the part that decides the predicate:
#
#   outcome                        model_id  model_group  custom_llm_provider  api_base
#   ------------------------------ --------- ------------ -------------------- --------
#   served / served from cache     set       set          set                  set
#   BOTH provider faults           set       set          EMPTY                EMPTY
#   RouterRateLimitError           EMPTY     set          EMPTY                EMPTY
#   every other refusal            EMPTY     EMPTY        EMPTY                EMPTY
#
#   Spend and tokens are real only on `served`; every other row above carries spend 0, and
#   `served from cache` carries real tokens at spend 0. That is why no version of this
#   changes SUM(spend) — see NEITHER OPTION TOUCHES MONEY below.
#
#   *  a /v1/embeddings call the fake provider has no route for: NotFoundError, a
#      different litellm exception class from the 500, and it populates llm_provider too.
#   ** deploy/gateway/require_principal.py, finding 36.
#   †  the FIRST refusal from a key with rpm_limit set comes from the router's own limiter
#      and the rest from the proxy's, so one operator-visible class writes two different
#      error_class values with two different typed-column shapes. Recorded because a
#      predicate tuned to one of them would have looked right on a four-request sample.
#
# THE PREMISE THIS CORRECTS. It was recorded as measured that over-budget and
# model-not-permitted refusals "never reach the ledger at all". They do. The earlier
# measurement looked at the REFUSED USER'S line on the bill, saw the right number there,
# and concluded no row existed — but those rows are written with no key alias, so they
# land under `(unattributed)` where nobody was looking. `(unattributed)` is the bucket
# finding 36 exists to empty; counting refusals refills it with a phantom principal.
#
# So `status = 'failure'` is not "the provider did not answer". It is "this request did
# not succeed", and it spans two things an operator must never see added together:
#
#   THE PROVIDER FAILED     the gateway admitted the request, called an upstream, and got
#                           nothing back. The provider may well have charged for the
#                           attempt while LiteLLM recorded spend 0, so the count is the
#                           only signal that would let anyone notice that against an
#                           invoice. This is a fault, and it is ours to see.
#   THE GATEWAY REFUSED     the gateway declined the request itself — no principal, over
#                           budget, not entitled to the model, rate limited, no such
#                           model. No upstream was called, so no money could have been
#                           spent by anyone. This is the layer WORKING, and counting it as
#                           usage means the bill grows when a user is DENIED service.
#
# THE DISCRIMINATOR IS TWO INDEPENDENT SIGNALS THAT MUST AGREE, and it is a conjunction
# rather than a single test because each one alone has a reachable hole that the other
# covers. A refusal has to be proven by BOTH:
#
#   s.model_id = ''    NO ROUTER DEPLOYMENT WAS SELECTED. A typed first-class column, not a
#                      JSON shape. litellm writes it from `metadata['model_info']['id']`,
#                      which the Router stamps onto the request data at the moment it picks
#                      a deployment — so a non-empty value means an upstream was addressed.
#                      Measured on the bundle, whole-ledger cross-tab: set on all served
#                      rows, on cache hits, and on BOTH provider-fault classes
#                      (InternalServerError, NotFoundError); empty on all six refusal
#                      classes (HTTPException 403 and 429, BudgetExceededError,
#                      ProxyException ×2, RouterRateLimitError).
#   error names no     litellm's own exception classes carry the provider they were raised
#   provider           for; the proxy-level refusals are FastAPI `HTTPException`,
#                      `BudgetExceededError`, `ProxyException`, `ProxyModelNotFoundError`
#                      and `RouterRateLimitError`, none of which has that attribute, so
#                      `get_error_information` falls back to `""` (vendor:
#                      litellm_logging.py `getattr(original_exception, "llm_provider", "")`).
#
# Not `error_code` — a gateway rate limit and a provider rate limit are both 429. And not
# `model_group`: measured, `RouterRateLimitError` carries a model_group, so it is set on a
# refusal and cannot separate them.
#
# `api_base` AND `custom_llm_provider` LOOK LIKE THE RIGHT ANSWER AND ARE THE WRONG ONE.
# They are first-class typed columns beside `model_id`, they are populated on every served
# row, and they are empty on every refusal row — a cross-tab that says "use these" if the
# only two classes you sample are served and refused. They are ALSO empty on every provider
# fault, so requiring them to be populated in order to count a request would classify every
# upstream failure as a refusal and delete it from `requests`: the exact defect reason 3
# below is about, arrived at by a measurement that never sampled a fault.
#
# That is structural, not a sampling artifact, and it is worth stating because the columns
# will keep inviting this. In `proxy_track_cost_callback.async_post_call_failure_hook` the
# kwargs handed to `get_logging_payload` are the raw inbound `request_data` plus a synthetic
# `litellm_params` holding only `proxy_server_request` and `metadata`. `get_logging_payload`
# then reads `api_base` from `litellm_params` and `custom_llm_provider` from the top-level
# kwargs — neither of which that synthetic dict has. So both columns are unconditionally
# empty on EVERY failure row, refusal and fault alike, and carry no signal here at all.
# `model_id` survives the same path only because it is read from `metadata`, which the
# failure hook merges the request's real metadata into.
#
# WHICH WAY IT FAILS IF THE SHAPE EVER CHANGES. A refusal must be POSITIVELY PROVEN, on
# both signals. Anything unrecognised counts as admitted and, if it failed, as a provider
# fault. That direction over-reports faults; the other direction deletes requests from the
# count, which is this codebase's signature defect and the thing reason 3 below is about.
# The over-reporting direction is also the LOUD one: the refusal classes are pinned by
# TestARefusalIsNotAFailedRequestAndIsNotUsage, so a predicate that drifted into admitting
# refusals fails CI, whereas a predicate that drifted into deleting requests would pass
# every test that only ever looks at rows it kept.
#
# THE RULING (enterpriseaiframework-e69, and the founder may reverse it — see finding 41).
# `requests` counts every request the gateway ADMITTED, and each way of costing nothing
# gets its own named subtotal beside it. It does NOT become a count of requests that
# succeeded, and it does NOT count a request the gateway refused — a refusal was never
# admitted, so it is reported BESIDE `requests` as `refused_requests` rather than inside
# it. Three numbers, and the arithmetic is closed:
#
#   requests           = admitted             = answered + failed_requests
#   cached_requests    ⊆ requests             answered without calling a provider
#   failed_requests    ⊆ requests             a provider was called and did not answer
#   refused_requests   ∩ requests = ∅         the gateway declined; nothing was called
#
# The losing option was to subtract failures out of `requests`, which is cheaper and reads
# better — "4 requests, $0.01" becomes "1 request, $0.01" and every number on the line then
# refers to the same event. It was rejected for three reasons.
#
#   1. It is lossy and irreversible. failed_requests lets anyone who wants the net count
#      compute `requests - failed_requests`; subtracting at the source destroys the
#      failure count for every consumer downstream, and nothing can recover it.
#   2. It would give the column two rules. d58 already decided that a cache hit — the
#      other $0 row — stays in `requests` and is explained by `cached_requests`. Excluding
#      one kind of zero while keeping the other means `requests` answers a different
#      question depending on which zero you have, which is how the next correction gets
#      applied to only half the cases. It already happened twice to the attribution join
#      (see ledger_attribution_sql above).
#   3. A vanishing request is this codebase's signature defect. If a provider starts
#      erroring on half its traffic, subtracting failures makes the operator's graph show
#      a quiet drop in usage — indistinguishable from people using it less — while the
#      surviving rows all look healthy. The count is the ONLY trace a failure leaves: it
#      has no spend and no tokens to show up in.
#
# NEITHER OPTION TOUCHES MONEY, which is what makes this decidable at all. Every class
# above except a served request carries spend 0, so SUM(s.spend) is byte-identical under
# both options AND under the refusal split — the cent-level agreement with the provider's
# own invoice (finding 9) is not on the table here and no version of this trades it. The
# sums below are therefore taken over EVERY row, refusals included, deliberately: if a
# refusal ever did carry spend, that is money and it must appear, not be filtered out by a
# predicate written when refusals were free.
#
# AND THE SECOND SIGNAL MOVES NO OPERATOR-VISIBLE NUMBER EITHER, measured rather than argued.
# One SELECT over the 238-row ledger a full `make test` leaves behind, which holds all nine
# classes, classifying every row under the previous single-signal predicate and under this
# two-signal one and reporting both:
#
#   requests 121 / 121   failed_requests 36 / 36   refused_requests 117 / 117   cached 13
#   SUM(spend) over admitted rows = SUM(spend) over all rows = 0.013947
#
# Identical on every column. That is the property this correction should have: the rows it
# moves are shapes no class in the bundle produces, so it closes a hole without restating
# anybody's bill — and it is also why the change cannot be justified by better numbers. The
# rows it moves are enumerated and executed in tests/test_ledger_row_shapes.py, which is
# where the evidence for the change lives.
#
# For contrast, on the same ledger, the api_base/custom_llm_provider predicate rejected
# above: requests 121 -> 85, refused 117 -> 153, and failed_requests 36 -> ZERO. The one
# number this whole item exists to create, reading nothing for ever, on a ledger holding 36
# real upstream failures.
#
# Read against `status`, not against spend or tokens. That is not hypothetical: measured on
# this bundle's ledger, `spend = 0` matches 166 rows — 13 cache hits AND all 153 failure rows
# — so deducing "failed" from the money column would report cache hits as failures and vice
# versa depending on which way it was written. `total_tokens = 0` matches those same 153
# failures and cannot separate them either, and would sweep in anything else that ever
# records no tokens.
#
# The observed values are exactly 'success' and 'failure'. The COALESCE is defensive rather
# than measured: nothing here has produced a NULL status, but the sibling column cache_hit
# does carry the literal string 'None' on older rows, so this family of columns clearly
# does not guarantee a clean value. Failing to 'not a failure' is the right direction —
# the bill should under-claim failures rather than invent them.
_FAILED = "lower(COALESCE(s.status, '')) = 'failure'"

_ERROR_INFO = "s.metadata->'error_information'"

# SIGNAL 1 — litellm's error object exists and explicitly names NO provider.
#
# PROVEN, NOT INFERRED FROM AN ABSENCE, and that is the whole difference between this and
# the version it replaces. The earlier form asked "does `->>'llm_provider'` come back NULL",
# which is TRUE for three different situations that mean three different things:
#
#   {"llm_provider": ""}     the measured refusal shape                  -> a refusal
#   {"error_class": ...}     the key is absent; we know nothing about it  -> NOT a refusal
#   {"llm_provider": null}   the key is present and explicitly null       -> NOT a refusal
#
# The last two were being counted as refusals, which means a request that really happened
# was deleted from `requests` — reason 3's defect, in the code written to prevent it. Not
# hypothetical: `llm_provider` is `Optional[str]` in litellm's own
# `StandardLoggingPayloadErrorInformation`, and `get_error_information` fills it with
# `getattr(original_exception, "llm_provider", "")`, which returns None — serialised as JSON
# null — for any exception whose constructor defaults that argument to None. `ImageFetchError`
# in litellm/exceptions.py is exactly such a class, and it is a PROVIDER fault: a request
# the gateway admitted and forwarded. So the null shape is reachable on the one class of row
# that must never be dropped.
#
# Hence three positive conditions rather than one negative one: the object must be an
# object, the key must be present AND a JSON string, and that string must be empty. Any
# other shape — absent key, JSON null, a nested object, a bare string where the object
# should be — is unrecognised and therefore not a proven refusal.
_ERROR_NAMES_NO_PROVIDER = f"""jsonb_typeof({_ERROR_INFO}) = 'object'
    AND jsonb_typeof({_ERROR_INFO}->'llm_provider') = 'string'
    AND {_ERROR_INFO}->>'llm_provider' = ''"""

# SIGNAL 2 — the router never selected a deployment, so nothing was addressed upstream.
# A typed column rather than a JSON shape; see the long note above for what it covers, what
# it does not, and why its neighbours `api_base` and `custom_llm_provider` cannot be used.
_NO_DEPLOYMENT_SELECTED = "COALESCE(s.model_id, '') = ''"

# The gateway declined this request itself. No upstream was called.
#
# THE COALESCE IS THE WHOLE PREDICATE'S CORRECTNESS, not a tidy-up. Every `jsonb_typeof()`
# above is NULL when the thing it is given does not exist, and NULL propagates through AND —
# so on a failure row carrying no `error_information` at all this expression is NULL rather
# than FALSE. `COUNT(*) FILTER (WHERE ...)` counts only rows where the condition is TRUE, so
# without the COALESCE such a row falls out of `refused_requests` AND, through `NOT (...)`,
# out of `requests` as well: it vanishes off the bill entirely. That was a real defect in an
# earlier draft, found by running the predicate over synthetic rows rather than by reading
# it, and tests/test_ledger_row_shapes.py now executes exactly those rows against exactly
# this SQL so it cannot come back. Defaulting to FALSE makes the row admitted and, since it
# failed, a provider fault — over-reporting faults instead of deleting requests.
_REFUSED = f"""COALESCE({_FAILED}
    AND {_NO_DEPLOYMENT_SELECTED}
    AND {_ERROR_NAMES_NO_PROVIDER}, false)"""

# The gateway let this request through. Everything that is not a proven refusal, so an
# unrecognised row is counted rather than silently dropped from the bill.
_ADMITTED = f"NOT {_REFUSED}"

# An admitted request the provider did not answer.
_PROVIDER_FAILED = f"({_FAILED} AND {_ADMITTED})"


def refused_sql() -> str:
    """The one definition of "the gateway declined this request", as SQL.

    Exposed for the same reason ledger_attribution_sql is: the per-request CSV the exit
    path hands a departing customer has to label rows by the SAME rule the aggregate bill
    counts them by, or the archive and the bill disagree about which requests were
    refusals. That exact drift has happened twice to the attribution join. Assumes the
    ledger is aliased `s`, as ledger_attribution_sql()["join"] does.
    """
    return _REFUSED


def failed_sql() -> str:
    """`status = 'failure'`, refusals included. Pair with refused_sql() to separate them."""
    return _FAILED


def admitted_sql() -> str:
    """"The gateway let this request through" — what `requests` counts."""
    return _ADMITTED


def provider_failed_sql() -> str:
    """"Admitted, and the provider did not answer" — what `failed_requests` counts."""
    return _PROVIDER_FAILED


def cache_hit_sql() -> str:
    """"Answered without calling a provider" — what `cached_requests` counts."""
    return _CACHE_HIT


# The columns the four predicates above actually read. Named here so a test can build a
# table of synthetic rows that is complete with respect to the rule rather than with respect
# to whatever the test author happened to think of — if the predicate grows a term on a new
# column, this list is what makes the omission visible. tests/test_ledger_row_shapes.py
# asserts it against the SQL.
PREDICATE_COLUMNS = ("status", "cache_hit", "model_id", "metadata")


async def spend_by_user_and_surface(since: str | None = None) -> list[dict]:
    """The single query the scope item names. One row per (user, surface)."""
    where, params = "", []
    if since:
        where = 'WHERE s."startTime" >= $1::text::timestamptz'
        params.append(since)

    params.append(SHARED_SURFACE_ALIASES)
    attr = ledger_attribution_sql(f"${len(params)}")
    sql = f"""
    SELECT
        {attr["principal"]} AS username,
        COALESCE({attr["surface"]}, '(unknown)') AS surface,
        COUNT(*) FILTER (WHERE {_ADMITTED})::bigint AS requests,
        COUNT(*) FILTER (WHERE {_CACHE_HIT})::bigint AS cached_requests,
        COUNT(*) FILTER (WHERE {_PROVIDER_FAILED})::bigint AS failed_requests,
        COUNT(*) FILTER (WHERE {_REFUSED})::bigint AS refused_requests,
        COALESCE(SUM(s.spend), 0)::float8         AS spend,
        COALESCE(SUM(s.prompt_tokens), 0)::bigint AS prompt_tokens,
        COALESCE(SUM(s.completion_tokens), 0)::bigint AS completion_tokens
    {attr["join"]}
    {where}
    GROUP BY 1, 2
    ORDER BY spend DESC, username, surface
    """
    p = await pool()
    async with p.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return [dict(r) for r in rows]


async def totals(since: str | None = None) -> dict:
    where, params = "", []
    if since:
        where = 'WHERE s."startTime" >= $1::text::timestamptz'
        params.append(since)
    sql = f"""
    SELECT COUNT(*) FILTER (WHERE {_ADMITTED})::bigint AS requests,
           COUNT(*) FILTER (WHERE {_CACHE_HIT})::bigint AS cached_requests,
           COUNT(*) FILTER (WHERE {_PROVIDER_FAILED})::bigint AS failed_requests,
           COUNT(*) FILTER (WHERE {_REFUSED})::bigint AS refused_requests,
           COALESCE(SUM(s.spend), 0)::float8 AS spend,
           COUNT(DISTINCT {_ALIAS}) AS active_keys
    {_LEDGER_JOIN}
    {where}
    """
    p = await pool()
    async with p.acquire() as conn:
        row = await conn.fetchrow(sql, *params)
    return dict(row)


async def unpriced_models(since: str | None = None) -> list[dict]:
    """Models that consumed tokens but recorded no spend.

    A model absent from the gateway's price map still serves traffic and still counts
    tokens — it just prices every request at zero. Budgets therefore never trip and the
    bill silently under-reports. Nothing errors, which is what makes it dangerous.

    This is the leak detector (design §2.5) reduced to the one case this row can check
    without invoice reconciliation.
    """
    # Cache hits are excluded: they cost nothing upstream, so $0 against counted tokens is
    # correct rather than a missing price. Counting them here made the detector fire on
    # healthy traffic, and a detector that cries wolf is one people stop reading — which
    # is exactly how a genuinely unpriced model would then slip through.
    where = f"WHERE s.total_tokens > 0 AND NOT ({_CACHE_HIT})"
    params: list = []
    if since:
        where += ' AND s."startTime" >= $1::text::timestamptz'
        params.append(since)
    sql = f"""
    SELECT s.model,
           COUNT(*)                                  AS requests,
           COALESCE(SUM(s.total_tokens), 0)::bigint  AS tokens,
           COALESCE(SUM(s.spend), 0)::float8         AS spend
    FROM "LiteLLM_SpendLogs" s
    {where}
    GROUP BY s.model
    HAVING COALESCE(SUM(s.spend), 0) = 0
    ORDER BY tokens DESC
    """
    p = await pool()
    async with p.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return [dict(r) for r in rows]


async def ledger_ready() -> bool:
    """True once the gateway has created its tables. Used by readiness, not liveness."""
    try:
        p = await pool()
        async with p.acquire() as conn:
            return bool(await conn.fetchval("SELECT to_regclass('public.\"LiteLLM_SpendLogs\"')"))
    except Exception:
        return False
