"""What a spend row's principal is called — the rule, in isolation from the database.

Finding 34: `/admin/spend` showed `6a67b18069dba4d1126fef44 / chat  135 req  $0.2247`
while the portal, over the same query and the same money, showed `baron`. The lookup was
never broken; it was applied at two call sites in the portal and nowhere else.

The tests below pin the naming rule itself. The companion integration test
(`TestChatPrincipalOnTheOneBill` in test_scope_items.py) proves the wiring end to end
against the running bundle, with a real ObjectId in a real ledger row — these cover the
cases that are impractical to stage there: an unreachable chat database, one person with
two chat accounts, and every principal that must NOT be touched.

No mocks of the module under test. The only thing substituted is the contents of the
id -> username map, which is exactly what a Mongo read would have populated, and it is
set through the module's own cache so the code path under test is the real one.
"""

import sys
from pathlib import Path

import pytest

CONTROL_PLANE = Path(__file__).resolve().parent.parent / "control-plane"
sys.path.insert(0, str(CONTROL_PLANE))

from app import chat_identity  # noqa: E402

BARON_ID = "6a67b18069dba4d1126fef44"
OTHER_ID = "6a680dd2d6a3e58bd5596392"


@pytest.fixture()
def mapped(monkeypatch):
    """The map as a successful Mongo read would leave it: two chat accounts, one person.

    `_loaded` is set so nothing here reaches for a database — CHAT_MONGO_URL is unset in
    a test process anyway, so `refresh()` is a no-op that returns 0 and leaves the cache
    alone. Both are asserted below rather than assumed.
    """
    monkeypatch.setattr(chat_identity, "_cache", {BARON_ID: "baron", OTHER_ID: "baron"})
    monkeypatch.setattr(chat_identity, "_loaded", True)
    return chat_identity


@pytest.fixture()
def unreachable(monkeypatch):
    """The map as an unreachable or unconfigured chat database leaves it: empty."""
    monkeypatch.setattr(chat_identity, "_cache", {})
    monkeypatch.setattr(chat_identity, "_loaded", True)
    return chat_identity


def test_a_known_chat_user_is_named_not_shown_as_hex(mapped):
    """The defect, at its smallest: this is what /admin/spend printed for 135 requests."""
    assert mapped.principal_label(BARON_ID) == "baron"


def test_the_hex_form_does_not_survive_anywhere_in_the_label(mapped):
    """A label that merely *contains* a name is not a fix. The ObjectId must be gone."""
    label = mapped.principal_label(BARON_ID)
    assert BARON_ID not in label, (
        f"the raw ObjectId is still on the bill inside {label!r}"
    )


def test_an_unresolvable_chat_id_is_labelled_as_such_never_dropped_or_guessed(unreachable):
    """The honest case. The chat database is down; we still owe the operator the money.

    Three wrong answers this rules out: dropping the row (money vanishes from the bill),
    inventing a name (attribution becomes fiction), and printing the bare ObjectId (which
    reads as a person's name and is what finding 34 actually was).
    """
    label = unreachable.principal_label(BARON_ID)
    assert unreachable.is_unresolved(label), label
    assert label != BARON_ID, "a bare ObjectId in a username column is not a label"
    assert BARON_ID in label, (
        "the identifier must survive inside the label — it is the only handle an "
        "operator has for tracing whose spend this was"
    )


def test_an_unreachable_chat_database_does_not_fail_the_bill(unreachable):
    """chat_identity's degradation contract: losing a name costs a label, not the bill."""
    rows = [{"username": BARON_ID, "surface": "chat", "requests": 135,
             "spend": 0.224679, "prompt_tokens": 10, "completion_tokens": 20}]
    out = unreachable.attribute(rows)
    assert len(out) == 1
    assert out[0]["requests"] == 135
    assert out[0]["spend"] == pytest.approx(0.224679)


def test_refresh_without_a_configured_mongo_is_a_no_op(monkeypatch):
    """Asserted rather than assumed, because the fixtures above depend on it: with no
    CHAT_MONGO_URL a refresh must return empty-handed instead of raising or blocking."""
    monkeypatch.setattr(chat_identity, "_MONGO_URL", "")
    assert chat_identity.refresh() == 0


# --------------------------------------------------------------------------
# The rows this change must NOT touch. Every one of these was correct before
# the fix and is the thing most likely to be broken by it.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("principal", ["baron", "student", "claire", "attrtest-9f2c1a"])
def test_a_per_user_key_principal_passes_through_untouched(mapped, principal):
    """The IDE and terminal surfaces hold per-user keys; the alias already names the
    person. `baron / ide  223 req` was right on the old bill and must still be right."""
    assert mapped.principal_label(principal) == principal


def test_the_unattributed_bucket_keeps_its_name(mapped):
    """Finding 25's remainder — master-key calls that belong to no principal. It is a
    real category, not a failure of this lookup, and must not be relabelled."""
    assert mapped.principal_label("(unattributed)") == "(unattributed)"
    assert mapped.principal_label("") == "(unattributed)"


def test_the_shared_chat_key_is_not_presented_as_a_person(mapped):
    """Rows the chat surface wrote without forwarding a user — conversation titling, for
    instance. `chat-surface` beside real names invites somebody to read it as one."""
    assert mapped.principal_label("chat-surface") == "(chat surface, no user)"


def test_labelling_is_idempotent(mapped, unreachable):
    """A second application must not corrupt a name. This is the property that makes it
    safe for the query to name principals while old callers may still be doing so too."""
    for raw in [BARON_ID, "baron", "", "chat-surface", "(unattributed)"]:
        once = mapped.principal_label(raw)
        assert mapped.principal_label(once) == once, raw
    once = unreachable.principal_label(BARON_ID)
    assert unreachable.principal_label(once) == once


def test_a_thirty_two_character_hex_string_is_not_a_chat_id(mapped):
    """Only a 24-character ObjectId is ours to translate. Anything else in end_user came
    from somewhere we do not speak for, and rewriting it would be inventing attribution."""
    not_an_id = "a" * 32
    assert mapped.principal_label(not_an_id) == not_an_id


# --------------------------------------------------------------------------
# One row per (principal, surface), still, after translation
# --------------------------------------------------------------------------

def test_two_chat_accounts_for_one_person_merge_into_one_row(mapped):
    """Translation can make two rows the same row. The query promises one row per
    (principal, surface); if it emitted `baron / chat` twice, one reader would sum them
    and another would show the first — which is how two renderings start disagreeing
    again, one level down from finding 34."""
    rows = [
        {"username": BARON_ID, "surface": "chat", "requests": 135, "spend": 0.2,
         "prompt_tokens": 10, "completion_tokens": 20},
        {"username": OTHER_ID, "surface": "chat", "requests": 21, "spend": 0.0034,
         "prompt_tokens": 1, "completion_tokens": 2},
    ]
    out = mapped.attribute(rows)
    chat = [r for r in out if r["surface"] == "chat"]
    assert len(chat) == 1, f"expected one chat row for baron, got {out}"
    assert chat[0]["username"] == "baron"
    assert chat[0]["requests"] == 156
    assert chat[0]["spend"] == pytest.approx(0.2034)
    assert chat[0]["prompt_tokens"] == 11
    assert chat[0]["completion_tokens"] == 22


def test_merging_does_not_fold_different_surfaces_together(mapped):
    """`by user AND by surface` is the scope item. One person's chat and ide spend are
    two rows, and staying two rows is the whole breakdown."""
    rows = [
        {"username": BARON_ID, "surface": "chat", "requests": 135, "spend": 0.22},
        {"username": "baron", "surface": "ide", "requests": 223, "spend": 1.2256},
    ]
    out = mapped.attribute(rows)
    assert {(r["username"], r["surface"]) for r in out} == {("baron", "chat"),
                                                            ("baron", "ide")}
    assert [r["requests"] for r in out if r["surface"] == "ide"] == [223]


def test_the_total_is_unchanged_by_naming(mapped):
    """Whatever the labels do, the money must add up to what came out of the database."""
    rows = [
        {"username": BARON_ID, "surface": "chat", "requests": 135, "spend": 0.224679},
        {"username": OTHER_ID, "surface": "chat", "requests": 21, "spend": 0.0034},
        {"username": "(unattributed)", "surface": "(unknown)", "requests": 42,
         "spend": 0.1133},
        {"username": "student", "surface": "ide", "requests": 38, "spend": 0.0532},
    ]
    out = mapped.attribute(rows)
    assert sum(r["requests"] for r in out) == sum(r["requests"] for r in rows)
    assert sum(r["spend"] for r in out) == pytest.approx(sum(r["spend"] for r in rows))


def test_rows_come_back_ordered_by_spend(mapped):
    """The bill is read top-down by an operator asking who is expensive."""
    rows = [
        {"username": "student", "surface": "ide", "requests": 38, "spend": 0.0532},
        {"username": BARON_ID, "surface": "chat", "requests": 135, "spend": 0.224679},
    ]
    out = mapped.attribute(rows)
    assert [r["username"] for r in out] == ["baron", "student"]
