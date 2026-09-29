"""A user can get a key from the portal without an operator (enterpriseaiframework-1b4).

Observed on the freerouter backend: "Your API keys — No keys issued yet" and nothing to press.
Two defects stacked. `freerouter.list_keys` returned raw rollup rows (field `name`, spent
accounts only), so `my_keys` matched none of them; and the page only offers an action on a
listed row, so an empty list meant no way to mint. These pin both, plus the one surface a user
must NOT rotate themselves (chat — its key is operator-seeded into LibreChat).
"""

import asyncio
import importlib
import sys
import types
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if not hasattr(sys.modules.get("app.gateway"), "parse_alias"):
    sys.modules.pop("app.gateway", None)
    importlib.import_module("app.gateway")
for name in ("app.db", "app.metering", "app.issuance", "app.chat_identity"):
    if name not in sys.modules:
        sys.modules[name] = types.ModuleType(name)

from app import freerouter, portal  # noqa: E402


def _keys(monkeypatch, listed: list[dict], user: str = "dana") -> dict[str, dict]:
    async def fake_list_keys():
        return listed
    monkeypatch.setattr(portal.provisioning, "list_keys", fake_list_keys)
    rows = asyncio.run(portal.my_keys(user=user))["keys"]
    return {r["surface"]: r for r in rows}


def test_a_user_with_no_keys_still_gets_a_row_per_surface_to_act_on(monkeypatch):
    rows = _keys(monkeypatch, [])
    assert set(rows) == {"api", "chat", "ide", "terminal"}
    assert all(not r["issued"] for r in rows.values())
    assert rows["ide"]["alias"] == "dana::ide"


def test_issued_keys_are_marked_and_other_users_keys_are_not_shown(monkeypatch):
    rows = _keys(monkeypatch, [
        {"key_alias": "dana::terminal", "spend": 1.5, "max_budget": None},
        {"key_alias": "evan::ide", "spend": 9.0, "max_budget": None},
    ])
    assert rows["terminal"]["issued"] is True
    assert rows["ide"]["issued"] is False, "evan's ide key must not count as dana's"
    assert all(r["alias"].startswith("dana::") for r in rows.values())


def test_chat_is_listed_but_not_self_service(monkeypatch):
    rows = _keys(monkeypatch, [])
    assert rows["chat"]["self_service"] is False
    assert rows["ide"]["self_service"] and rows["terminal"]["self_service"]


def test_rotating_the_chat_key_is_refused(monkeypatch):
    async def must_not_issue(*a, **k):
        raise AssertionError("issued a chat key from the portal")
    monkeypatch.setattr(portal.issuance, "issue", must_not_issue, raising=False)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(portal.rotate_my_key({"surface": "chat"}, user="dana"))
    assert exc.value.status_code == 400


def test_freerouter_list_keys_reports_unspent_accounts_in_the_callers_shape(monkeypatch):
    """The freerouter backend must answer in `key_alias` terms and include accounts that have
    never spent — the rollup alone has neither."""
    async def live():
        return {"dana::ide": "acct-new", "dana::terminal": "acct-spent"}

    class Resp:
        def json(self):
            return {"data": [{"name": "dana::terminal", "account_id": "acct-spent",
                              "spend_micro": 2_500_000}]}

    async def fake_request(method, path, **kw):
        return Resp()

    monkeypatch.setattr(freerouter, "_account_ids_by_alias", live)
    monkeypatch.setattr(freerouter, "_request", fake_request)
    keys = {k["key_alias"]: k for k in asyncio.run(freerouter.list_keys())}
    assert set(keys) == {"dana::ide", "dana::terminal"}
    assert keys["dana::ide"]["spend"] == 0.0
    assert keys["dana::terminal"]["spend"] == 2.5


def test_the_api_key_is_self_service_and_separate_from_the_workspace_keys(monkeypatch):
    """Using the platform from outside must never mean rotating a key a workspace holds."""
    rows = _keys(monkeypatch, [{"key_alias": "dana::ide", "spend": 0.0, "max_budget": None}])
    assert rows["api"]["self_service"] is True
    assert rows["api"]["alias"] == "dana::api"
    assert rows["api"]["issued"] is False and rows["ide"]["issued"] is True


def test_the_api_surface_round_trips_but_is_never_auto_provisioned():
    from app import gateway
    assert gateway.parse_alias("dana::api") == ("dana", "api")
    assert gateway.surface_alias("dana", "api") == "dana::api"
    # SURFACES is what the IdP sync mints for every user; an external key must be asked for.
    assert "api" not in gateway.SURFACES
