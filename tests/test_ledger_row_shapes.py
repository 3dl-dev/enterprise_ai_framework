"""Every ledger row shape, run through the real bill predicates in the real Postgres.

enterpriseaiframework-e69, finding 41.

WHY THIS FILE EXISTS AT ALL. `requests`, `cached_requests`, `failed_requests` and
`refused_requests` are four SQL predicates over one table, and the operator's whole picture
of who was served — and of who was DENIED — is the partition they induce. The integration
tests in tests/test_scope_items.py drive real traffic through the gateway and check the
resulting numbers, which is the right way to prove the classes the bundle can actually
produce. It is structurally incapable of proving anything about the classes it cannot: a
row whose error object carries no provider key, a row whose provider is explicitly JSON
null, a row with no error object at all, a NULL where a text column was expected. Those
shapes decide whether a real request is silently deleted from the bill, and every one of
them was reasoned about in a comment instead of executed.

The reasoning was wrong twice. Once when `jsonb_typeof()` of an absent key returned NULL
and `COUNT(*) FILTER` counted only TRUE, so a failure row with no error object fell out of
`refused_requests` and, through `NOT (...)`, out of `requests` too — it vanished off the
bill. And again when "the key came back NULL" was treated as "the error names no provider",
which folded two unknown shapes (key absent, key explicitly null) in with the one measured
refusal shape (key present and empty) and classified a genuine provider fault as a refusal.
Both were found by running the predicate over synthetic rows. This is that run, committed,
so it guards the partition instead of living in someone's scratch directory.

HOW IT IS HERMETIC. It creates a TEMP table inside the bundle's Postgres session, inserts
rows into it, and evaluates `metering`'s own exported SQL against it. It never reads or
writes `LiteLLM_SpendLogs`; the temp table dies with the psql process. The predicates are
imported from the module the control plane runs, not copied — a copy would pass forever
after the original changed.

WHAT THIS DOES NOT PROVE, and what does. It does not prove which shape any real class
produces; that is measured by TestARefusalIsNotAFailedRequestAndIsNotUsage and
TestFailedRequestsAreNamedNotErased against live traffic. The MEASURED block below encodes
what those runs observed so the two halves cannot drift apart, but the authority for
"litellm writes this shape for that class" is the live test, not this file.
"""

import json
import re
import subprocess
import sys
import types
from pathlib import Path

import pytest

from conftest import BUNDLE, _load_env

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "control-plane"))

# Same shell as control-plane/tests/test_export_attribution.py, and for the same reason:
# `metering` owns the expressions under test and must be imported for real, but it binds
# asyncpg at import and the test venv deliberately carries no driver. Nothing here opens a
# connection through it — the database is reached with psql, over compose.
if "asyncpg" not in sys.modules:
    _pg = types.ModuleType("asyncpg")
    _pg.Pool = object

    async def _create_pool(*a, **kw):  # pragma: no cover - never reached
        raise RuntimeError("this suite talks to postgres with psql, not asyncpg")

    _pg.create_pool = _create_pool
    sys.modules["asyncpg"] = _pg

from app import metering  # noqa: E402

# The four numbers the bill reports, as the SQL the bill actually uses.
PREDICATES = {
    "admitted": metering.admitted_sql(),
    "refused": metering.refused_sql(),
    "failed": metering.provider_failed_sql(),
    "cached": metering.cache_hit_sql(),
}


# --------------------------------------------------------------------------------------
# The rows.
#
# `expect` is (admitted, refused, failed, cached). A row is named for the situation that
# produces it and carries WHY the expected classification is the safe one, because the
# expectation is the entire content of this file — a wrong expectation here reads as green.
# --------------------------------------------------------------------------------------

def _err(**fields) -> str:
    """litellm's error_information object, as it lands in metadata."""
    return json.dumps({"error_information": fields})


def _err_raw(value: str) -> str:
    """metadata with error_information set to some arbitrary JSON value."""
    return '{"error_information": %s}' % value


DEPLOYMENT = "7cadb74b793b7836aac203e50eb163a2b78dfe6fa0dd489530f93c41c539bb7a"

# MEASURED — the nine classes observed on the bundle, one fresh key per class, dumped raw.
# Column values are transcribed from that whole-ledger cross-tab; see
# metering._REFUSED's note for the table.
MEASURED = [
    # name, status, cache_hit, model_id, metadata, expect
    ("served", "success", "None", DEPLOYMENT, "{}", (True, False, False, False)),
    ("served from cache", "success", "True", DEPLOYMENT, "{}", (True, False, False, True)),
    ("upstream 500", "failure", "False", DEPLOYMENT,
     _err(llm_provider="openai", error_class="InternalServerError", error_code="500"),
     (True, False, True, False)),
    ("upstream has no such route", "failure", "False", DEPLOYMENT,
     _err(llm_provider="openai", error_class="NotFoundError", error_code="404"),
     (True, False, True, False)),
    ("the router's own rate limit", "failure", "False", "",
     _err(llm_provider="", error_class="RouterRateLimitError", error_code=""),
     (False, True, False, False)),
    ("the proxy's own rate limit", "failure", "False", "",
     _err(llm_provider="", error_class="HTTPException", error_code="429"),
     (False, True, False, False)),
    ("no attributable principal", "failure", "False", "",
     _err(llm_provider="", error_class="HTTPException", error_code="403"),
     (False, True, False, False)),
    ("over the key's budget", "failure", "False", "",
     _err(llm_provider="", error_class="BudgetExceededError", error_code=""),
     (False, True, False, False)),
    ("model not on the key's list", "failure", "False", "",
     _err(llm_provider="", error_class="ProxyException", error_code="401"),
     (False, True, False, False)),
]

# UNMEASURED — shapes no class in this bundle produces, which is exactly why they need
# executing. Each one is a way for the partition to be wrong in silence.
HAZARDS = [
    # A failure row carrying no error object at all. The first draft of the refusal
    # predicate evaluated NULL here, and `COUNT(*) FILTER` counts only TRUE, so the row
    # left `refused_requests` AND `requests` and vanished off the bill.
    ("failure with no error object", "failure", "False", "", "{}",
     (True, False, True, False)),
    # The error object exists and the provider key is ABSENT. Unknown, not proven — and
    # `->>'llm_provider'` returns NULL for this exactly as it does for the measured
    # refusal shape, which is how the two came to be treated as one thing.
    ("error object, provider key absent", "failure", "False", "",
     _err(error_class="SomethingNew", error_code="500"), (True, False, True, False)),
    # The provider key is present and explicitly JSON null. REACHABLE ON A FAULT:
    # llm_provider is Optional[str] in litellm's own StandardLoggingPayloadErrorInformation
    # and get_error_information fills it with getattr(exc, "llm_provider", ""), which
    # yields None for any exception defaulting that argument to None — ImageFetchError in
    # litellm/exceptions.py is one, and it is raised on a request that WAS forwarded.
    ("provider key explicitly null", "failure", "False", "",
     _err_raw('{"llm_provider": null, "error_class": "ImageFetchError"}'),
     (True, False, True, False)),
    # A provider name that is not a string. Unrecognised shape, so not proven.
    ("provider key is not a string", "failure", "False", "",
     _err_raw('{"llm_provider": {"name": "openai"}}'), (True, False, True, False)),
    # error_information is a bare JSON string rather than an object.
    ("error object is a bare string", "failure", "False", "",
     _err_raw('"upstream exploded"'), (True, False, True, False)),
    # error_information is JSON null.
    ("error object is json null", "failure", "False", "", _err_raw("null"),
     (True, False, True, False)),
    # metadata itself is SQL NULL.
    ("metadata is sql null", "failure", "False", "", None, (True, False, True, False)),
    # THE TWO SIGNALS DISAGREE, and this row is why the predicate is a conjunction. The
    # error object is the exact measured refusal shape, but a deployment WAS selected, so
    # something upstream was addressed. Admitted: the direction that over-reports faults.
    ("deployment selected, error names no provider", "failure", "False", DEPLOYMENT,
     _err(llm_provider="", error_class="HTTPException", error_code="429"),
     (True, False, True, False)),
    # model_id SQL NULL rather than empty string — the column is nullable, so the COALESCE
    # on it is load-bearing and this row is what makes it so.
    ("model_id is sql null", "failure", "False", None,
     _err(llm_provider="", error_class="BudgetExceededError"),
     (False, True, False, False)),
    # status in another case. lower() is what makes this a refusal rather than a success.
    ("status is upper case", "FAILURE", "False", "",
     _err(llm_provider="", error_class="BudgetExceededError"),
     (False, True, False, False)),
    # status SQL NULL. Not a failure, so not a refusal either — the bill under-claims
    # failures rather than inventing them.
    ("status is sql null", None, "False", "", _err(llm_provider=""),
     (True, False, False, False)),
    # cache_hit SQL NULL on a served row. LiteLLM leaves it NULL on paths that predate the
    # column, and a boolean cast would have errored here rather than returning false.
    ("cache_hit is sql null", "success", None, DEPLOYMENT, "{}",
     (True, False, False, False)),
    # cache_hit is the literal string 'None', which is what a served row actually carries.
    ("cache_hit is the string None", "success", "None", DEPLOYMENT, "{}",
     (True, False, False, False)),
]

ROWS = MEASURED + HAZARDS


def _classify() -> dict[str, dict[str, bool | None]]:
    """Build the temp table, evaluate the real predicates, return {name: {pred: value}}.

    One psql process, because a TEMP table is scoped to its session — which is also what
    makes this hermetic. NULL is returned as None rather than coerced, because "the
    predicate was NULL for this row" is a failure mode this file exists to catch and
    coercing it would hide exactly that.
    """
    env = _load_env()
    values = []
    for name, status, cache_hit, model_id, metadata, _ in ROWS:
        def lit(v, cast=""):
            return "NULL" + cast if v is None else "$lit$" + v + "$lit$" + cast
        values.append(
            f"({lit(name)}, {lit(status)}, {lit(cache_hit)}, {lit(model_id)}, "
            f"{lit(metadata, '::jsonb')})"
        )
    selects = ",\n           ".join(
        f"({sql}) AS {name}" for name, sql in PREDICATES.items()
    )
    sql = f"""
    CREATE TEMP TABLE shapes (
        name text, status text, cache_hit text, model_id text, metadata jsonb
    );
    INSERT INTO shapes (name, status, cache_hit, model_id, metadata) VALUES
    {",".join(values)};
    SELECT s.name,
           {selects}
    FROM shapes s;
    """
    proc = subprocess.run(
        ["docker", "compose", "-f", str(BUNDLE / "docker-compose.yml"),
         "--env-file", str(BUNDLE / ".env"), "exec", "-T", "postgres",
         "psql", "-U", env["POSTGRES_USER"], "-d", "gateway",
         "-v", "ON_ERROR_STOP=1", "--no-align", "--tuples-only",
         "--field-separator=\t", "-f", "-"],
        input=sql, capture_output=True, text=True,
    )
    assert proc.returncode == 0, (
        f"the bill's own predicates would not even execute against a ledger-shaped "
        f"table:\n{proc.stderr}\n{proc.stdout}"
    )
    out = {}
    for line in proc.stdout.splitlines():
        if not line.strip() or "\t" not in line:
            continue
        parts = line.split("\t")
        if len(parts) != 1 + len(PREDICATES):
            continue
        name, rest = parts[0], parts[1:]
        out[name] = {
            p: (None if v == "" else v == "t") for p, v in zip(PREDICATES, rest)
        }
    return out


@pytest.fixture(scope="module")
def classified():
    got = _classify()
    missing = [r[0] for r in ROWS if r[0] not in got]
    assert not missing, f"psql returned no verdict for {missing}"
    return got


@pytest.mark.parametrize("row", ROWS, ids=[r[0] for r in ROWS])
def test_the_bill_classifies_this_row_shape_exactly_once_and_correctly(row, classified):
    name, _status, _cache, _model_id, _metadata, expect = row
    got = classified[name]
    want = dict(zip(("admitted", "refused", "failed", "cached"), expect))

    # TOTALITY FIRST. A NULL predicate is not a wrong answer, it is no answer, and
    # `COUNT(*) FILTER (WHERE ...)` silently drops the row from the number — which is how a
    # real request disappears off a bill without anything failing.
    for pred, value in got.items():
        assert value is not None, (
            f"`{pred}` evaluated to NULL for the row shape '{name}'. COUNT(*) FILTER "
            f"counts only TRUE, so this row is absent from that number entirely — and if "
            f"the NULL one is `refused`, NOT(NULL) is NULL too and the row falls out of "
            f"`requests` as well. It vanishes off the bill: {got}"
        )

    assert got == want, (
        f"row shape '{name}' is classified {got}, expected {want}.\n"
        f"  admitted -> is it inside `requests`\n"
        f"  refused  -> the gateway declined it; must be disjoint from `requests`\n"
        f"  failed   -> admitted and the provider did not answer\n"
        f"A row that flipped from admitted to refused has been DELETED from the operator's "
        f"request count, which is the defect finding 41 exists to prevent."
    )


def test_no_row_is_both_admitted_and_refused(classified):
    """The one invariant the ruling states as set arithmetic: refused ∩ requests = ∅."""
    both = [n for n, v in classified.items() if v["admitted"] and v["refused"]]
    neither = [n for n, v in classified.items() if not v["admitted"] and not v["refused"]]
    assert not both, f"counted as usage AND as a refusal, so the totals overcount: {both}"
    assert not neither, (
        f"counted as neither, so these rows are on no operator surface at all: {neither}"
    )


def test_every_failure_is_either_a_fault_or_a_refusal_and_never_both(classified):
    """`failed_requests` ⊆ `requests`, and the two kinds of failure partition the failures.

    Asserted from the row table rather than from the SQL, because the claim is about the
    partition the operator sees and not about how it is spelled.
    """
    for name, status, _cache, _model_id, _metadata, _expect in ROWS:
        if (status or "").lower() != "failure":
            continue
        v = classified[name]
        assert v["failed"] != v["refused"], (
            f"'{name}' is a failure that is {'both' if v['failed'] else 'neither'} a "
            f"provider fault and a gateway refusal: {v}"
        )
        if v["failed"]:
            assert v["admitted"], (
                f"'{name}' is counted in failed_requests but not in requests, so "
                f"requests - failed_requests underflows: {v}"
            )


def test_a_success_is_never_a_failure_of_either_kind(classified):
    """The path this item did not change, asserted rather than assumed.

    Every previous pass on this predicate broke something on the success side while fixing
    the failure side — cached_requests once absorbed failures because it was loosened to
    `spend = 0`. A served row and a cache hit must stay outside both failure numbers.
    """
    for name, status, _cache, _model_id, _metadata, _expect in ROWS:
        if (status or "").lower() != "success":
            continue
        v = classified[name]
        assert v["admitted"], f"'{name}' succeeded and is not counted as a request: {v}"
        assert not v["failed"], f"a served request is reported as a provider fault: {v}"
        assert not v["refused"], f"a served request is reported as refused: {v}"


def test_the_two_signals_are_both_load_bearing(classified):
    """Neither half of the conjunction is decoration — collapsing either one breaks a row.

    Without the `model_id` signal, 'deployment selected, error names no provider' would be
    a refusal and a request that reached an upstream would leave the count. Without the
    error-object signal, 'model_id is sql null' — a budget refusal — would be admitted and
    billed as a provider fault. Both rows are in the table above; this test states why they
    are, so that removing one is a visible decision rather than a tidy-up.
    """
    assert classified["deployment selected, error names no provider"]["admitted"], (
        "the model_id signal is doing nothing: a row where the router DID select a "
        "deployment is being classified from the error object alone"
    )
    assert classified["model_id is sql null"]["refused"], (
        "the error-object signal is doing nothing: a budget refusal with no deployment is "
        "no longer recognised as a refusal"
    )


def test_the_predicates_read_no_column_the_row_table_does_not_carry():
    """The completeness check this file needs to keep being complete.

    Rows above are built from metering.PREDICATE_COLUMNS. If a predicate grows a term on a
    new ledger column, every row here silently gets the column's DEFAULT and the new term
    is never exercised — the table would still be green and would no longer be a proof.
    This fails instead, and names the column to add.
    """
    every = " ".join(PREDICATES.values())
    # Discovered from the SQL rather than checked against a list this test curated: a
    # curated list is one more place to forget the new column. `\w+` and not a substring
    # test, because `s.model_id` contains `s.model` and a substring test reports a column
    # the predicate never reads.
    read = set(re.findall(r"\bs\.(\w+)", every))
    declared = set(metering.PREDICATE_COLUMNS)
    assert read == declared, (
        f"the bill's predicates read {sorted(read)} but metering.PREDICATE_COLUMNS "
        f"declares {sorted(declared)}. Every row in this file is built from the declared "
        f"list, so an undeclared column is one nothing here varies — the shape proof has a "
        f"hole in it. Add the column to PREDICATE_COLUMNS and to the row table."
    )
    # And the two columns that must NOT be read, because they are unconditionally empty on
    # every failure row (litellm's failure hook builds the payload from the inbound request
    # data, which has neither), so requiring them populated would classify every provider
    # fault as a refusal. Measured; see metering._REFUSED's note.
    for forbidden in ("api_base", "custom_llm_provider"):
        assert f"s.{forbidden}" not in every, (
            f"the bill is deciding who was refused using s.{forbidden}, which litellm "
            f"leaves empty on EVERY failure row — refusals and genuine provider faults "
            f"alike. Every failed request would be reclassified as a refusal and dropped "
            f"out of the operator's request count."
        )
