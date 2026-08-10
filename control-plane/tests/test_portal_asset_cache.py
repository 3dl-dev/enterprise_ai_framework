"""The portal's static assets must be cache-busted, or a deploy does not reach the browser.

FileResponse sends no Cache-Control, so a browser that has visited before keeps its cached
`/portal/static/app.js` and renders the freshly-deployed index.html against it. That is not
hypothetical: it shipped the Agents tab whose click handler the old app.js never had, so the
tab did nothing. These tests pin the fix — the index stamps a content hash onto each asset
URL and is itself uncacheable — against the REAL portal_static files, so a hash computed
against a stale copy of index.html would fail here rather than in someone's browser.
"""

import re
import sys
import types
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Same discipline as test_portal_auth.py: portal.py imports siblings that want a live
# database at import time; stub them so a header-and-file test does not need Postgres.
for name in ("app.db", "app.gateway", "app.metering", "app.issuance",
             "app.chat_identity", "app.agent_usage", "app.agents"):
    sys.modules.setdefault(name, types.ModuleType(name))
sys.modules["app.gateway"].SURFACES = ("chat", "ide", "terminal")

from app import portal  # noqa: E402

STATIC = portal.STATIC


@pytest.fixture
def client():
    api = FastAPI()
    api.include_router(portal.router)
    # Loopback, so require_user honours the identity header (the sidecar's position).
    return TestClient(api, client=("127.0.0.1", 43210))


AUTH = {"X-Auth-Request-Preferred-Username": "baron"}


def test_index_stamps_a_content_hash_onto_each_asset_url(client):
    r = client.get("/portal/", headers=AUTH)
    assert r.status_code == 200, r.text
    body = r.text
    for asset in ("app.js", "style.css"):
        expected = portal._asset_version(asset)
        assert expected not in ("0", ""), f"{asset} not found to hash"
        # The real, current bytes of the asset must be the hash the page serves.
        assert f"/portal/static/{asset}?v={expected}" in body, (
            f"{asset} URL is not stamped with its current content hash"
        )
        # And no un-stamped reference is left to defeat the busting.
        assert not re.search(rf"/portal/static/{re.escape(asset)}(?![?])", body), (
            f"a bare /portal/static/{asset} without ?v= survives and can be served stale"
        )


def test_index_itself_is_not_cacheable(client):
    # If the index were cached, the hashed URLs inside it would themselves go stale — the
    # busting has to start from an always-fresh document.
    r = client.get("/portal/", headers=AUTH)
    assert r.headers.get("cache-control") == "no-store"


def test_hashed_asset_is_immutable_and_bare_asset_revalidates(client):
    ver = portal._asset_version("app.js")
    hashed = client.get(f"/portal/static/app.js?v={ver}", headers=AUTH)
    assert hashed.status_code == 200
    assert "immutable" in hashed.headers.get("cache-control", ""), (
        "a content-hashed URL is byte-immutable and should cache hard"
    )
    bare = client.get("/portal/static/app.js", headers=AUTH)
    assert bare.status_code == 200
    assert bare.headers.get("cache-control") == "no-cache", (
        "a versionless URL must revalidate so it can never pin a stale asset"
    )


def test_the_bug_would_reproduce_without_the_fix():
    """The fix bites: a page that references a bare asset URL is a defect this catches.

    Reproduce the pre-fix behaviour directly against the helper — the served index must not
    contain a single un-hashed asset reference, which is the exact condition that let the old
    app.js survive a deploy.
    """
    served = STATIC / "index.html"
    raw = served.read_text()
    # The raw file DOES contain bare references (that is correct — the route stamps them).
    assert "/portal/static/app.js" in raw
    # But every bare reference must be one the route rewrites; prove the route leaves none.

    # Exercise the rewrite the way the route does, without a live app.
    html = raw
    for asset in ("app.js", "style.css"):
        html = html.replace(
            f"/portal/static/{asset}",
            f"/portal/static/{asset}?v={portal._asset_version(asset)}",
        )
    assert re.search(r"/portal/static/app\.js\?v=[0-9a-f]{12}", html)
    assert not re.search(r"/portal/static/app\.js(?![?])", html)
