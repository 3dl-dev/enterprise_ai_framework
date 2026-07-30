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
#   outcome                        status    llm_provider  spend  tokens  alias on row
#   ------------------------------ --------- ------------- -----  ------  ------------
#   served                         success   (no error)    real   real    yes
#   served from cache              success   (no error)    0      real    yes
#   upstream 500                   failure   openai        0      0       yes
#   upstream refused the call *    failure   openai        0      0       yes
#   no attributable principal **   failure   ''            0      0       NO
#   rate limit the GATEWAY imposed failure   ''            0      0       yes
#   model not on the key's list    failure   ''            0      0       NO
#   over the key's budget          failure   ''            0      0       NO
#   model not in the catalogue     failure   ''            0      0       yes
#
#   *  a /v1/embeddings call the fake provider has no route for: NotFoundError, a
#      different litellm exception class from the 500, and it populates llm_provider too.
#   ** deploy/gateway/require_principal.py, finding 36.
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
# THE DISCRIMINATOR is `metadata->'error_information'->>'llm_provider'`. litellm's own
# exception classes carry the provider they were raised for; the proxy-level refusals are
# FastAPI `HTTPException`, `BudgetExceededError`, `ProxyException` and
# `ProxyModelNotFoundError`, which have no such attribute, so the field serialises as the
# empty string. Measured non-vacuously: two different provider exception classes populate
# it (InternalServerError, NotFoundError) and four different refusal classes leave it
# empty. Not `error_code` — a gateway rate limit and a provider rate limit are both 429.
#
# WHICH WAY IT FAILS IF THE SHAPE EVER CHANGES. A refusal must be positively proven: the
# error object must exist AND name no provider. Anything unrecognised counts as admitted
# and, if it failed, as a provider failure. That direction over-reports faults; the other
# direction would delete requests from the count, which is this codebase's signature
# defect and the thing reason 3 below is about.
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
# Read against `status`, not against spend or tokens. That is not hypothetical: measured on
# this bundle's ledger, `spend = 0` matches the cache-hit row AND all ten failure rows, so
# deducing "failed" from the money column would report cache hits as failures and vice
# versa depending on which way it was written. `total_tokens = 0` is no better — it would
# sweep in anything else that ever records no tokens.
#
# The observed values are exactly 'success' and 'failure'. The COALESCE is defensive rather
# than measured: nothing here has produced a NULL status, but the sibling column cache_hit
# does carry the literal string 'None' on older rows, so this family of columns clearly
# does not guarantee a clean value. Failing to 'not a failure' is the right direction —
# the bill should under-claim failures rather than invent them.
_FAILED = "lower(COALESCE(s.status, '')) = 'failure'"

# The provider named on a failure, or NULL if none was. `jsonb_typeof(...) = 'object'` is
# not decoration: without it a malformed `error_information` — a bare JSON string, say —
# would extract as NULL and be read as "no provider named", i.e. as a refusal, which
# subtracts a request that really happened. With it, only a well-formed error object that
# explicitly names no provider can classify a row as refused.
_ERROR_INFO = "s.metadata->'error_information'"
_ERROR_PROVIDER = f"""CASE WHEN jsonb_typeof({_ERROR_INFO}) = 'object'
    THEN NULLIF({_ERROR_INFO}->>'llm_provider', '') END"""

# The gateway declined this request itself. No upstream was called.
#
# THE COALESCE IS THE WHOLE PREDICATE'S CORRECTNESS, not a tidy-up. `jsonb_typeof()` of an
# absent key is NULL, so on a failure row with no `error_information` at all this expression
# evaluates to NULL — and `COUNT(*) FILTER (WHERE ...)` counts only rows where the condition
# is TRUE. Without the COALESCE such a row is excluded from `refused_requests` AND, through
# `NOT (...)`, from `requests` as well: it vanishes off the bill entirely, which is the
# precise defect reason 3 above is about, reintroduced by the fix for it. Found by running
# the predicate over synthetic rows rather than by reading it. Defaulting to FALSE makes the
# row admitted and, since it failed, a provider fault — the direction that over-reports
# faults instead of deleting requests.
_REFUSED = f"""COALESCE({_FAILED}
    AND jsonb_typeof({_ERROR_INFO}) = 'object'
    AND {_ERROR_PROVIDER} IS NULL, false)"""

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
