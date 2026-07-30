"""Where the portal's own front doors point.

WHY THIS IS NOT A PRESENCE CHECK

The account menu is the only route from a surface back to the portal's own pages — the
account console, the user's published work, sign-out. Every one of them is populated from
this payload, and every one of them carries target=_blank, so nothing in the page ever
follows them. "The href is not empty" is therefore satisfied by a link to another tenant's
realm, to a hostname the deployment stopped using, or to somebody else's published site.
That is a route back that looks present and is wrong, which is worse than one that is
visibly missing.

So this asserts the URLs themselves, per authenticated user, plus the case nobody changes:
an unconfigured deployment must emit nothing rather than a half-formed URL that resolves
against whatever origin the page happens to be on.
"""

import asyncio
import sys
import types
from pathlib import Path

import pytest
from starlette.requests import Request

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Same trade as test_portal_auth.py: the siblings want a live database at import time and
# none of them is on this path. A test that needs Postgres to prove a URL is a test nobody
# runs.
for name in ("app.db", "app.gateway", "app.metering", "app.issuance", "app.chat_identity"):
    if name not in sys.modules:
        sys.modules[name] = types.ModuleType(name)
sys.modules["app.gateway"].SURFACES = ("chat", "ide", "terminal")

from app import portal  # noqa: E402

BASE = "https://ai.example.test"
REALM = "the-configured-realm"


def _request(email: str = "") -> Request:
    headers = [(b"x-auth-request-email", email.encode())] if email else []
    return Request({"type": "http", "method": "GET", "path": "/portal/api/me",
                    "headers": headers, "client": ("127.0.0.1", 0), "query_string": b""})


def _links(user: str, email: str = "") -> dict:
    return asyncio.run(portal.me(_request(email), user=user))["links"]


@pytest.fixture()
def configured(monkeypatch):
    monkeypatch.setattr(portal, "PUBLIC_BASE_URL", BASE)
    monkeypatch.setattr(portal, "IDP_REALM", REALM)


def test_every_link_is_the_exact_url_it_is_supposed_to_be(configured):
    links = _links("dana")
    assert links == {
        "chat": BASE,
        # Always the same path for everybody: the proxy picks the pod from the
        # authenticated name, so nothing per-user belongs in this URL.
        "workspace": "/workshop/",
        "published": f"{BASE}/live/dana/",
        "account": f"{BASE}/realms/{REALM}/account",
        # The page prefers this one for the account menu item, so losing the fragment
        # silently downgrades "change your password" to "here is a console".
        "password": f"{BASE}/realms/{REALM}/account#/security/signingin",
        "signout": "/portal/oauth2/sign_out",
    }


def test_the_realm_comes_from_configuration_and_is_not_baked_in(monkeypatch):
    """A deployment that renames its realm must not keep handing out the old one."""
    monkeypatch.setattr(portal, "PUBLIC_BASE_URL", BASE)
    monkeypatch.setattr(portal, "IDP_REALM", "renamed")
    assert _links("dana")["account"] == f"{BASE}/realms/renamed/account"


def test_the_published_link_belongs_to_the_caller_and_nobody_else(configured):
    """The wrong-tenant case, stated as a test.

    Two users, one process. Each one's route to their own work must name themselves; a
    payload that leaked the other's path would still pass any check that only asks whether
    the link is non-empty.
    """
    dana, evan = _links("dana"), _links("evan")
    assert dana["published"] == f"{BASE}/live/dana/"
    assert evan["published"] == f"{BASE}/live/evan/"
    assert dana["published"] != evan["published"]
    for other in ("evan",):
        assert other not in dana["published"], f"dana's link mentions {other}"


def test_an_unconfigured_deployment_emits_nothing_rather_than_half_a_url(monkeypatch):
    """The case nobody changes, and the one that fails dangerously.

    With no PUBLIC_BASE_URL, the obvious implementation produces "/realms/x/account" and
    "/live/dana/" — root-relative URLs that resolve against whatever origin the page is
    served from. In an embedded surface that is a link into the wrong site that a presence
    check calls fine. Empty is the correct answer: the page disables the item.
    """
    monkeypatch.setattr(portal, "PUBLIC_BASE_URL", "")
    monkeypatch.setattr(portal, "IDP_REALM", REALM)
    links = _links("dana")
    assert links["account"] == ""
    assert links["password"] == ""
    assert links["published"] == ""
    # Chat degrades to the origin root, which is where the chat surface is served.
    assert links["chat"] == "/"
    # These two are relative by design — they are paths on this very origin.
    assert links["workspace"] == "/workshop/"
    assert links["signout"] == "/portal/oauth2/sign_out"


def test_the_email_comes_from_the_proxys_header_and_is_never_invented(configured):
    """The menu head shows it under the username, so a wrong one is a wrong identity."""
    assert asyncio.run(portal.me(_request("dana@example.test"), user="dana"))["email"] \
        == "dana@example.test"
    assert asyncio.run(portal.me(_request(), user="dana"))["email"] == ""
