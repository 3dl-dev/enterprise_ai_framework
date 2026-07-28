"""The portal, driven the way a person drives it.

Signs in through Keycloak with a real account and checks that what comes back is that
account's own data and nobody else's. The point of the portal is that one login reaches
every surface, so a test that skipped the login would be testing the wrong thing.
"""

import base64
import re
import subprocess

import httpx
import pytest

NS = "enterprise-ai"
_FORM = re.compile(r'<form[^>]*\baction="([^"]+)"[^>]*\bmethod="post"', re.I | re.S)


def _secret(name: str, key: str) -> str:
    out = subprocess.run(
        ["kubectl", "-n", NS, "get", "secret", name, "-o", f"jsonpath={{.data.{key}}}"],
        capture_output=True, text=True, timeout=60, check=True,
    ).stdout
    return base64.b64decode(out).decode()


@pytest.fixture(scope="module")
def base_url() -> str:
    return _secret("enterprise-ai-secrets", "PUBLIC_BASE_URL").rstrip("/")


@pytest.fixture(scope="module")
def creds() -> tuple[str, str]:
    # The live-test account, not an operator. If the portal only worked for the person
    # who built it, that would pass a test written with the wrong account.
    return (_secret("workspace-user-student", "USERNAME"),
            _secret("workspace-user-student", "PASSWORD"))


@pytest.fixture(scope="module")
def client(base_url, creds) -> httpx.Client:
    user, password = creds
    c = httpx.Client(verify=False, follow_redirects=True, timeout=60.0)

    landing = c.get(f"{base_url}/portal/")
    assert landing.status_code == 200, f"portal did not load a login: {landing.status_code}"
    assert "password" in landing.text.lower(), (
        f"expected Keycloak's login form, landed on {landing.url}"
    )
    m = _FORM.search(landing.text)
    assert m, "no login form found on the identity provider page"
    action = m.group(1).replace("&amp;", "&")

    done = c.post(action, data={"username": user, "password": password},
                  headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert done.status_code == 200, f"login POST returned {done.status_code}"
    assert "password" not in done.text.lower() or "Everything in one place" in done.text, (
        "still on a login form after submitting credentials — the login did not take"
    )
    return c


def test_the_page_loads_after_one_login(client, base_url):
    r = client.get(f"{base_url}/portal/")
    assert r.status_code == 200
    assert "Everything in one place" in r.text


def test_assets_are_served(client, base_url):
    for path in ("/portal/static/style.css", "/portal/static/app.js"):
        r = client.get(f"{base_url}{path}")
        assert r.status_code == 200 and len(r.content) > 500, f"{path} -> {r.status_code}"


def test_me_returns_the_signed_in_user(client, base_url, creds):
    r = client.get(f"{base_url}/portal/api/me")
    assert r.status_code == 200, r.text[:300]
    d = r.json()
    assert d["username"] == creds[0], f"portal thinks we are {d['username']}"
    links = d["links"]
    # Every front door the portal exists to gather up.
    for key in ("chat", "workspace", "published", "account", "password", "signout"):
        assert key in links, f"missing link: {key}"
    assert links["account"], "the account console link is empty — that panel is the reason to have it"


def test_spend_is_only_this_users(client, base_url, creds):
    r = client.get(f"{base_url}/portal/api/spend")
    assert r.status_code == 200, r.text[:300]
    d = r.json()
    assert d["username"] == creds[0]
    assert d["total"]["spend"] >= 0
    for row in d["by_surface"]:
        assert row["surface"], "a spend row with no surface"


def test_keys_are_only_this_users(client, base_url, creds):
    r = client.get(f"{base_url}/portal/api/keys")
    assert r.status_code == 200, r.text[:300]
    for k in r.json()["keys"]:
        owner = k["alias"].split("::")[0]
        assert owner == creds[0], (
            f"the portal showed {creds[0]} a key belonging to {owner}"
        )
        assert "key" not in k and "token" not in k, "a key secret was served to the page"


def test_published_list_is_scoped(client, base_url, creds):
    r = client.get(f"{base_url}/portal/api/published")
    assert r.status_code == 200
    d = r.json()
    assert d["username"] == creds[0]
    for p in d["projects"]:
        assert f"/live/{creds[0]}/" in p["url"], f"link escapes this user: {p['url']}"


def test_the_admin_api_is_not_reachable_by_a_signed_in_browser(client, base_url):
    """Signing in to the portal must not confer operator capability.

    Asserting on the STATUS here is wrong and the first version of this test made that
    mistake: the origin's root is a single-page app that answers 200 with its own index
    for any unrouted path, so `/admin/spend` returns 200 without the control plane having
    seen the request at all. What distinguishes them is the body — the admin API answers
    JSON, the chat SPA answers HTML.
    """
    for path in ("/admin/spend", "/admin/keys", "/admin/audit", "/admin/export/keys"):
        r = client.get(f"{base_url}{path}", follow_redirects=False)
        assert "application/json" not in r.headers.get("content-type", ""), (
            f"{path} answered JSON to a portal session — the admin API is publicly routed "
            f"and authentication was treated as authorisation"
        )


def test_nothing_escapes_the_portal_prefix_into_the_admin_api(client, base_url):
    """Only /portal/* is routed to the portal's proxy. Prove the prefix cannot be climbed."""
    escapes = [
        "/portal/../admin/spend",
        "/portal/..%2fadmin%2fspend",
        "/portal/%2e%2e/admin/spend",
        "/portal/api/../../admin/spend",
        "/portal/static/../../admin/spend",
    ]
    for path in escapes:
        r = client.get(f"{base_url}{path}", follow_redirects=False)
        assert "application/json" not in r.headers.get("content-type", ""), (
            f"{path} reached the control plane's admin API"
        )


def test_portal_static_never_serves_source(client, base_url):
    """No traversal through the static handler reaches the application's own files.

    Deliberately asserts on the BODY rather than the status. Most of these never reach the
    handler at all — the reverse proxy normalises the path first and they land on the
    origin's single-page app, which answers 200. A status assertion would therefore fail
    on a request that was in fact refused, and would say nothing about the one outcome
    that matters: whether source ever comes back.
    """
    attempts = [
        "/portal/static/..%2f..%2fmain.py",
        "/portal/static/%2e%2e%2fportal.py",
        "/portal/static/../portal.py",
        "/portal/static/../../app/main.py",
        "/portal/static/....//portal.py",
    ]
    for path in attempts:
        r = client.get(f"{base_url}{path}", follow_redirects=False)
        body = r.text[:4000]
        assert "require_user" not in body and "CONTROL_PLANE_ADMIN_TOKEN" not in body, (
            f"{path} served application source"
        )
        assert not re.search(r"^\s*(def|import|from)\s", body, re.M) or "<!DOCTYPE" in body, (
            f"{path} returned something that looks like Python: {body[:200]}"
        )
