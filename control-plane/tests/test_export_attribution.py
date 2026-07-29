"""Who the exported ledger names, and where that rule comes from.

`spend.csv` is the artifact scope item 9 hands a customer who is leaving. It is the one
rendering of the ledger that outlives the deployment, and it was the least attributed of
the three, because it joined every row to `LiteLLM_VerificationToken` — a table the exit
path EMPTIES, by revoking every key, immediately before writing the archive. Measured on
the cluster: 265 of 477 exported rows with no principal at all, over a ledger whose bill
attributed all but 42.

Two properties are checked here, without a database, because they are the two that were
wrong: that the export takes its attribution from the same expression the bill does rather
than a fourth copy of it, and that the name a human reads is produced the same way the
portal produces it. The half that needs Postgres — that attribution actually survives a
revocation end to end — is `tests/test_scope_items.py::TestItem9Exit`.
"""

import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# The driver, not the rule. `metering` is imported FOR REAL here — it owns the expression
# under test, and stubbing it would leave this asserting against a fake — but it binds
# asyncpg at import, which the test venv deliberately does not carry (see run-tests.sh:
# the venv exists to prove behaviour, not to host a database). Nothing below calls a
# connection method, so the driver is a shell.
if "asyncpg" not in sys.modules:
    _pg = types.ModuleType("asyncpg")
    _pg.Pool = object

    async def _create_pool(*a, **kw):  # pragma: no cover - never reached
        raise RuntimeError("no database in this suite")

    _pg.create_pool = _create_pool
    sys.modules["asyncpg"] = _pg

# The control plane's own database module, likewise: export.py imports it for the audit
# chain, which is a different export and not this test's subject.
if "app.db" not in sys.modules:
    sys.modules["app.db"] = types.ModuleType("app.db")
sys.modules["app.db"].GENESIS_HASH = "0" * 64

from app import chat_identity, export, metering  # noqa: E402

CHAT_ID = "6a67b18069dba4d1126fef44"


@pytest.fixture(autouse=True)
def _identity_map(monkeypatch):
    """A chat identity map, as if LibreChat's Mongo had been read.

    Patched rather than started: the mapping this translates lives in another component's
    database, and a test that needs Mongo running to prove a CSV column is a test nobody
    runs. The lookup itself is exercised against the real thing by the live suite.
    """
    monkeypatch.setattr(chat_identity, "_cache", {CHAT_ID: "baron"})
    monkeypatch.setattr(chat_identity, "_loaded", True)


def _row(**over):
    # Mirrors the columns the query in export.spend_csv selects. `status` joined it for
    # enterpriseaiframework-e69: without it a $0 row the provider failed is
    # indistinguishable from a served row that was never priced, and `cache_hit` cannot
    # tell them apart because a failure carries 'False' exactly like an unpriced success.
    base = {
        "request_id": "req-1", "start_time": "2026-07-29T00:00:00+00:00",
        "end_time": "2026-07-29T00:00:01+00:00", "model": "fake-large",
        "key_alias": "baron::ide", "surface": "ide", "end_user": "",
        "principal": "baron", "status": "success", "spend": 0.001, "prompt_tokens": 1,
        "completion_tokens": 2, "total_tokens": 3, "cache_hit": "",
    }
    base.update(over)
    return base


def _as_dict(values):
    return dict(zip(export.SPEND_COLUMNS, values))


# ---------------------------------------------------------------------------
# The name a human reads
# ---------------------------------------------------------------------------

def test_a_chat_principal_is_named_and_the_raw_identifier_is_kept():
    """Both columns, and this is the deliberate decision, not an oversight.

    `end_user` is evidence — it is what the caller actually sent, and it is what an
    operator reconciles against a provider invoice after this platform is deleted. So it
    is never rewritten. `principal` is the reading, and without it the archive is a column
    of hex for the surface most people use.
    """
    out = _as_dict(export.spend_row(
        _row(key_alias="chat-surface::chat", surface="chat",
             end_user=CHAT_ID, principal=CHAT_ID)
    ))
    assert out["principal"] == "baron", (
        f"the exported ledger names the chat spender {out['principal']!r}; a departing "
        "customer's archive must not be a column of hex for the surface most people use"
    )
    assert out["end_user"] == CHAT_ID, (
        "end_user was rewritten. It is the raw request field and the only thing that can "
        "be reconciled against a provider invoice — resolve alongside it, never over it"
    )


def test_a_principal_that_is_already_a_name_is_left_exactly_alone():
    """The case this change did not intend to alter — every non-chat surface."""
    out = _as_dict(export.spend_row(_row()))
    assert out["principal"] == "baron"
    assert out["key_alias"] == "baron::ide"
    assert out["surface"] == "ide"
    assert out["end_user"] == ""


def test_an_unattributable_row_says_so_rather_than_going_blank():
    """A blank cell reads as "no data". The bill's own word for it reads as a fact."""
    out = _as_dict(export.spend_row(_row(
        key_alias="", surface="", principal=metering.UNATTRIBUTED
    )))
    assert out["principal"] == "(unattributed)"


def test_an_unresolvable_chat_id_keeps_the_identifier_rather_than_vanishing():
    """Money that was definitely spent stays on the record even when nameless.

    The chat database can be unreachable, or the account can be gone — this is an EXIT
    export, so both are ordinary. Dropping the row would hide real spend and inventing a
    name would be worse, so the identifier stands.
    """
    ghost = "6b" + "0" * 22
    out = _as_dict(export.spend_row(_row(
        key_alias="chat-surface::chat", surface="chat", end_user=ghost, principal=ghost
    )))
    assert out["principal"] == ghost
    assert out["spend"] == 0.001


def test_naming_never_reopens_the_chat_database_per_row(monkeypatch):
    """A departed tenant's export is nothing but cache misses.

    `resolve` re-reads Mongo on a miss so somebody who signed in a second ago still gets a
    name. Per row, across an export, that is one round trip per unresolvable id against a
    database that by definition may be gone — with a 1.5s connect timeout each. The map is
    loaded once by spend_csv; the row shaper must not re-load it.
    """
    calls = []
    monkeypatch.setattr(chat_identity, "refresh", lambda: calls.append(1) or 0)
    for i in range(50):
        export.spend_row(_row(request_id=f"req-{i}", principal="6c" + "0" * 22))
    assert calls == [], (
        f"shaping 50 rows hit the chat database {len(calls)} time(s); an export of a "
        "tenant whose chat accounts are gone would stall on every single row"
    )


# ---------------------------------------------------------------------------
# Where the rule comes from
# ---------------------------------------------------------------------------

def test_the_export_and_the_bill_share_one_attribution_expression():
    """The duplication is the defect, not the join.

    Finding 25 fixed the deleted-key join in `metering`; the export kept its own copy and
    stayed broken for another two findings. This asserts there is one expression rather
    than two that happen to agree today — a fourth copy is how this recurs.
    """
    attr = metering.ledger_attribution_sql("$1")
    src = (ROOT / "app" / "export.py").read_text()
    assert 'ledger_attribution_sql' in src, (
        "export.py builds its own attribution SQL again"
    )
    body = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    assert "LEFT JOIN" not in body, (
        "export.py writes its own join to the key table again. That table is DELETED from "
        "by revocation — including by the exit path, right before this export runs"
    )
    assert "user_api_key_alias" in attr["alias"], attr["alias"]


def test_the_shared_expression_survives_the_key_table_being_emptied():
    """The property, stated as SQL: the alias does not depend on the join surviving.

    `v.key_alias` is NULL for every row whose key has been revoked. If the metadata term
    is not first, the whole expression is NULL and the archive is anonymous.
    """
    attr = metering.ledger_attribution_sql("$1")
    alias = attr["alias"]
    assert alias.index("user_api_key_alias") < alias.index("v.key_alias"), (
        "the deleted-key join is consulted before the metadata LiteLLM stamps on the "
        "spend row itself; revocation would empty the export"
    )
    # Both the surface and the principal are derived from that alias, so both inherit it.
    assert attr["alias"] in attr["surface"]
    assert attr["alias"] in attr["principal"]


def test_a_per_user_key_still_cannot_name_somebody_else_in_the_export():
    """The control on the rule this change did not touch (finding: forged attribution).

    `end_user` is caller-supplied. Only a key minted AS a shared surface may speak for
    another person; everything else is attributed from its alias. Sharing the expression
    means the export inherits that, and if it ever stops inheriting it, the export becomes
    a place to forge attribution that the bill refuses.
    """
    principal = metering.ledger_attribution_sql("$7")["principal"]
    assert "$7::text[]" in principal, (
        "the shared-surface list is not bound as the parameter the caller declared"
    )
    assert principal.index("ANY($7::text[])") < principal.index("s.end_user"), (
        "end_user is read without first checking the key is a shared surface"
    )
    assert principal.index("s.end_user") < principal.index("split_part"), (
        "the alias is consulted before the shared-surface end_user; precedence inverted"
    )
    assert f"'{metering.UNATTRIBUTED}'" in principal, (
        "a row with neither is not labelled, so it would export as SQL NULL"
    )


def test_the_csv_header_carries_both_columns_in_a_stable_order():
    """A header is a contract with whatever the customer reads this file in."""
    assert export.SPEND_COLUMNS.index("end_user") + 1 == export.SPEND_COLUMNS.index("principal")
    for required in ("request_id", "key_alias", "surface", "spend", "total_tokens"):
        assert required in export.SPEND_COLUMNS, required


# ---------------------------------------------------------------------------
# Which $0 rows the archive can explain (enterpriseaiframework-e69 / finding 40)
# ---------------------------------------------------------------------------

def test_a_failed_row_is_distinguishable_from_a_free_one_in_the_archive():
    """The two kinds of $0 row must not read identically to a departing customer.

    The aggregate bill separates them with `failed_requests` and `cached_requests`. This
    file is per-request, so it has no counts — it separates them with `status`, and
    `cache_hit` alone provably cannot: an upstream failure carries cache_hit 'False',
    which is exactly what an unpriced success carries too.
    """
    failed = _as_dict(export.spend_row(_row(
        status="failure", spend=0, prompt_tokens=0, completion_tokens=0,
        total_tokens=0, cache_hit="False",
    )))
    cached = _as_dict(export.spend_row(_row(
        status="success", spend=0, cache_hit="True",
    )))
    unpriced = _as_dict(export.spend_row(_row(status="success", spend=0, cache_hit="False")))

    assert failed["status"] == "failure"
    assert cached["status"] == "success"

    # The load-bearing claim: status separates the failure from the unpriced success, and
    # cache_hit does not. If a future edit drops `status` from SPEND_COLUMNS, the second
    # assertion still holds and the first stops being checkable — hence both.
    assert failed["status"] != unpriced["status"], (
        "a failed request and an unpriced success are indistinguishable in the archive"
    )
    assert failed["cache_hit"] == unpriced["cache_hit"], (
        "this test's premise is stale: cache_hit now separates these two, so the argument "
        "for carrying `status` needs rechecking rather than this assertion relaxing"
    )
    # And the money is untouched by any of it — the reason this was decidable at all.
    assert failed["spend"] == 0 and cached["spend"] == 0
