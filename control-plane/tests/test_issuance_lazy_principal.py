"""Minting a key self-heals a missing principal from the IdP, in isolation from the world.

A user added to the identity provider after the last `/admin/sync` had no principal row,
so every self-service mint path — create an agent, rotate a key — failed with
`no such principal: <user> (run /admin/sync)`, an error that told the *end user* to run an
operator-only command. julie hit exactly this when she tried to create a bot.

The fix reconciles a single principal from the IdP on demand (issuance._resolve_principal
-> identity.get_user). These tests pin the rule that governs it:

  * a principal the IdP knows and has enabled is created on the spot and the mint proceeds;
  * a username the realm does not know still yields nothing — 404, and NOT with the old
    "run /admin/sync" advice a user could not act on;
  * a disabled account is refused, because the enabled gate is the whole point of disable;
  * a principal that already exists never reaches for the IdP at all.

The companion integration coverage (TestItem3 in tests/test_scope_items.py) drives these
paths against a running bundle. What is staged here is the branch impractical to stage
there: a principal absent from the mirror but present in identity.

Only `app.db` is substituted, and only because it imports asyncpg, which the test venv does
not install (see bundle/bin/run-tests.sh). The gateway, identity and issuance modules under
test are the real ones; the two network calls each makes are monkeypatched per test.
"""

import asyncio
import sys
import types
from pathlib import Path

import pytest
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# db imports asyncpg (a live Postgres driver) at module load; the test venv omits it on
# purpose. `pool` and `audit` are monkeypatched per test, so a stub module is enough.
sys.modules.setdefault("app.db", types.ModuleType("app.db"))

# A sibling suite (test_portal_auth) replaces app.gateway/app.issuance with bare stub
# modules for its own import graph. If it imported first, those stubs would shadow the real
# code this file is here to exercise, so drop any stub (a real module has __file__) and let
# the import below load the genuine ones.
for _name in ("app.gateway", "app.issuance"):
    _mod = sys.modules.get(_name)
    if _mod is not None and not hasattr(_mod, "__file__"):
        del sys.modules[_name]

from app import issuance  # noqa: E402


class FakeConn:
    """Answers the two reads `issue` makes and records the writes.

    `principal_row` is what the first `SELECT ... FROM principal` returns; None models the
    stale mirror. When it is None and the INSERT ... RETURNING runs, the row the IdP upsert
    would produce is returned and remembered.
    """

    def __init__(self, principal_row):
        self.principal_row = principal_row
        self.inserted_principal = False
        self.executed = []

    async def fetchrow(self, sql, *args):
        s = " ".join(sql.split())
        if s.startswith("SELECT id, idp_user_id, enabled FROM principal"):
            return self.principal_row
        if s.startswith("INSERT INTO principal"):
            self.inserted_principal = True
            # Positional args: idp_user_id, username, email, enabled.
            self.principal_row = {"id": 42, "idp_user_id": args[0], "enabled": args[3]}
            return self.principal_row
        if s.startswith("SELECT max_budget, status FROM virtual_key"):
            return None  # nothing to rotate: the first-mint case
        raise AssertionError(f"unexpected fetchrow: {s}")

    async def execute(self, sql, *args):
        self.executed.append(" ".join(sql.split()))
        return "INSERT 0 1"


class FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        conn = self._conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


@pytest.fixture()
def wired(monkeypatch):
    """Substitute every collaborator; return a dict a test sets `conn`/`idp_user` on."""
    state = {"conn": None, "idp_user": None}

    async def fake_pool():
        return FakePool(state["conn"])

    async def fake_get_user(username):
        u = state["idp_user"]
        return u if (u and u["username"] == username) else None

    async def fake_generate_key(**kwargs):
        return {"key": "sk-minted-abc", "token": "hash-of-minted"}

    async def fake_delete_by_aliases(aliases, *, missing_ok=False):
        return {}

    async def fake_audit(*args, **kwargs):
        return "audit-1"

    monkeypatch.setattr(issuance.db, "pool", fake_pool, raising=False)
    monkeypatch.setattr(issuance.db, "audit", fake_audit, raising=False)
    monkeypatch.setattr(issuance.identity, "get_user", fake_get_user)
    monkeypatch.setattr(issuance.gateway, "generate_key", fake_generate_key)
    monkeypatch.setattr(issuance.gateway, "delete_by_aliases", fake_delete_by_aliases)
    return state


def test_missing_principal_known_to_idp_is_created_and_the_key_is_minted(wired):
    wired["conn"] = FakeConn(principal_row=None)
    wired["idp_user"] = {
        "idp_user_id": "kc-julie", "username": "julie",
        "email": "julie@example.com", "enabled": True,
    }

    result = asyncio.run(issuance.issue("julie", "agents/bot-one", actor="julie"))

    assert wired["conn"].inserted_principal, "the principal was not reconciled from the IdP"
    assert result["key"] == "sk-minted-abc"
    assert result["username"] == "julie"
    assert result["rotated"] is False  # a first mint, not a rotation


def test_username_the_idp_does_not_know_still_yields_no_principal(wired):
    wired["conn"] = FakeConn(principal_row=None)
    wired["idp_user"] = None  # the realm has never heard of this name

    with pytest.raises(HTTPException) as ei:
        asyncio.run(issuance.issue("no-such-principal", "ide", actor="admin"))

    assert ei.value.status_code == 404
    # The old message sent the end user to an operator command; it must not return.
    assert "/admin/sync" not in ei.value.detail
    assert not wired["conn"].inserted_principal


def test_a_disabled_account_is_refused_even_though_the_idp_knows_it(wired):
    wired["conn"] = FakeConn(principal_row=None)
    wired["idp_user"] = {
        "idp_user_id": "kc-mallory", "username": "mallory",
        "email": None, "enabled": False,
    }

    with pytest.raises(HTTPException) as ei:
        asyncio.run(issuance.issue("mallory", "ide", actor="mallory"))

    assert ei.value.status_code == 409
    assert "disabled" in ei.value.detail


def test_an_existing_principal_never_touches_the_idp(wired, monkeypatch):
    """The common path: a synced user rotates a key. No IdP lookup, no upsert."""
    wired["conn"] = FakeConn(
        principal_row={"id": 7, "idp_user_id": "kc-baron", "enabled": True}
    )

    async def explode(username):
        raise AssertionError("get_user was called for an already-known principal")

    monkeypatch.setattr(issuance.identity, "get_user", explode)

    result = asyncio.run(issuance.issue("baron", "ide", actor="baron"))

    assert result["key"] == "sk-minted-abc"
    assert wired["conn"].inserted_principal is False
