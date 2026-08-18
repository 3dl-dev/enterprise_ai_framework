"""The in-product report surface — assembly, the operator endpoint, and the page.

Proves the operator can open the report, compare by a dimension, and switch the dimension to
re-render — the turnkey acceptance — without any database on the request path (the endpoint
reads the content-free records store). The true pixel render is a live browser test
(tests-live); here the data contract and the page wiring are pinned hermetically.
"""

import os
import sys
import types
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "analytics")
STORE = os.path.join(FIX, "records_store.jsonl")

# Same stubbing as test_portal_auth: portal pulls siblings that want a live database at
# import. The analytics report path (report -> slicing -> metrics) is pure and needs none.
for name in ("app.db", "app.gateway", "app.metering", "app.issuance", "app.chat_identity"):
    if name not in sys.modules:
        sys.modules[name] = types.ModuleType(name)
sys.modules["app.gateway"].SURFACES = ("chat", "ide", "terminal")

from app.analytics import report  # noqa: E402


# ---------------------------------------------------------------- assembly (pure)


def test_assemble_slices_and_lists_dimensions():
    turns, sessions = report.load_records(STORE)
    m = report.assemble(turns, sessions, dimension="surface", min_n=1)
    assert set(m["meta"]["groups"]) == {"terminal", "chat"}
    assert "surface" in m["dimensions"] and "model" in m["dimensions"]


def test_assemble_window_filter():
    turns, sessions = report.load_records(STORE)
    # the opencode turns are on 2026-08-01; a window before that yields nothing.
    m = report.assemble(turns, sessions, dimension="model", since="2020-01-01",
                        until="2020-12-31", min_n=1)
    assert m["meta"]["groups"] == []


def test_missing_store_is_empty_not_an_error():
    turns, sessions = report.load_records("/no/such/store.jsonl")
    assert (turns, sessions) == ([], [])
    m = report.assemble(turns, sessions, dimension="model", min_n=1)
    assert m["meta"]["groups"] == []


# ---------------------------------------------------------------- endpoint + page


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv(report.RECORDS_PATH_ENV, STORE)
    from app import portal

    app = FastAPI()
    app.include_router(portal.router)
    # stand in for the operator identity so the routes run; require_admin_user itself is
    # tested separately below.
    app.dependency_overrides[portal.require_admin_user] = lambda: "operator"
    return TestClient(app)


def test_endpoint_returns_sliced_metrics_and_dimension_switch_rerenders(client):
    by_surface = client.get("/portal/api/analytics?dimension=surface&min_n=1").json()
    assert set(by_surface["meta"]["groups"]) == {"terminal", "chat"}

    by_model = client.get("/portal/api/analytics?dimension=model&min_n=1").json()
    assert set(by_model["meta"]["groups"]) == {"glm-5.2", "gpt-fake"}
    # switching the dimension genuinely re-groups the same corpus.
    assert by_model["meta"]["groups"] != by_surface["meta"]["groups"]


def test_endpoint_rejects_unknown_dimension(client):
    assert client.get("/portal/api/analytics?dimension=bogus").status_code == 400


def test_page_is_served_with_the_selector_and_api_wiring(client):
    r = client.get("/portal/analytics")
    assert r.status_code == 200
    body = r.text
    assert 'id="dimension"' in body            # the slice selector
    assert "/portal/api/analytics" in body     # wired to the data route
    assert "no-store" in r.headers.get("cache-control", "")


def test_operator_only_console(monkeypatch):
    from app import portal

    monkeypatch.setattr(portal, "ADMINS", {"boss"})
    api = FastAPI()

    @api.get("/probe")
    def probe(request: Request):
        try:
            return {"user": portal.require_admin_user(request, user=_hdr_user(request))}
        except HTTPException as exc:
            return {"error": exc.status_code}

    def _hdr_user(request):
        return request.headers.get("X-Auth-Request-Preferred-Username") or ""

    c = TestClient(api, client=("127.0.0.1", 43210))
    assert c.get("/probe", headers={"X-Auth-Request-Preferred-Username": "boss"}).json() == {"user": "boss"}
    # a non-operator gets the 404 that hides the console's existence.
    assert c.get("/probe", headers={"X-Auth-Request-Preferred-Username": "camper"}).json() == {"error": 404}
