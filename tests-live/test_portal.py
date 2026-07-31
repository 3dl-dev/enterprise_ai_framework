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
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

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
    # Keyed on the tab bar, which is the shell of the signed-in app. The previous
    # version looked for the absence of the word "password" — which the account link in
    # the user menu now contains, so a perfectly good login read as a failure.
    assert 'id="tab-chat"' in done.text, (
        "still on a login form after submitting credentials — the login did not take"
    )
    return c


def test_bare_portal_path_reaches_a_working_front_door(base_url, creds):
    """enterpriseaiframework-03f (REMAINING 1): a person who types the address without the
    trailing-slash convention must not be handed a dead end. DRIVEN, not read from source.

    `control-plane/app/portal.py:77` issues a 307 from `/portal` to `/portal/` -- but that
    code can only run if the request for the bare path ever REACHES the control-plane pod.
    Measured against the live public origin it does not. The gateway VM's Caddy `:8081`
    block (`mainframe` repo, `cloud-init/gateway-vendor.yaml`, hand-extended beyond what
    that file has checked in -- its own tracked copy has no `/portal` route at all) routes
    with a `handle_path /portal/*`-shaped matcher: Caddy's path glob requires the LITERAL
    trailing slash to match, so a request for the bare path falls through to the catch-all
    `handle { reverse_proxy ... 30380 }` block -- CHAT's NodePort, not the control plane's.
    `portal.py`'s redirect never executes; this is exactly the "redirect swallowed by a
    proxy" scenario this item's own remaining findings named.

    Measured live 2026-07-31 with curl (`follow_redirects` off), for both surfaces the
    portal fronts:
        GET /portal    -> 200, LibreChat's OWN index.html (not a 307, no Location header)
        GET /portal/   -> 302, to Keycloak (the correct, intended shape)
        GET /workshop  -> 200, LibreChat's OWN index.html (identical bug, identical shape)
        GET /workshop/ -> 302, to Keycloak (control-plane's own route -- correct)

    curl alone cannot catch the actual user-facing failure, though: LibreChat's router is
    entirely client-side, so the HTTP layer really does answer 200 with valid HTML either
    way -- indistinguishable from a working SPA boot to anything that only reads status
    codes. What a human's browser does with that HTML once it boots at the URL `/portal`
    is render LibreChat's OWN unrecognised-route error boundary: "Oops! Something
    Unexpected Occurred ... Status:: 404 Not Found" -- no login form, no chat composer, no
    path forward except "Contact the Admin". Only a real, JS-executing browser observes
    this, so this test drives one instead of an HTTP client.
    """
    user, password = creds
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(ignore_https_errors=True, viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        try:
            page.goto(f"{base_url}/portal", wait_until="load", timeout=45000)

            try:
                page.wait_for_selector("input[name='username']", timeout=15000)
            except PlaywrightTimeoutError:
                body = page.evaluate(
                    "() => document.body ? document.body.innerText.slice(0, 500) : ''"
                )
                pytest.fail(
                    "bare /portal (no trailing slash) never presented a login form -- "
                    f"landed on {page.url!r} with body starting: {body!r}\n"
                    "See this test's docstring: the gateway VM's Caddy /portal/* matcher "
                    "requires the literal trailing slash and falls through to chat for "
                    "the bare path, so the app's own 307 in portal.py:77 never runs."
                )

            page.fill("input[name='username']", user)
            page.fill("input[name='password']", password)
            page.click("input[type='submit'], button[type='submit']")
            page.wait_for_load_state("load", timeout=45000)

            page.wait_for_selector("#tab-chat", timeout=20000)
            assert page.locator("#tab-chat").is_visible(), (
                "signed in after starting at bare /portal but never reached the tabbed "
                f"shell -- landed on {page.url!r} instead"
            )
        finally:
            ctx.close()
            b.close()


def test_the_page_loads_after_one_login(client, base_url):
    r = client.get(f"{base_url}/portal/")
    assert r.status_code == 200
    for marker in ('id="tab-chat"', 'id="tab-code"', 'id="dlg-settings"'):
        assert marker in r.text, f"the signed-in shell is missing {marker}"


def test_the_workshop_is_served_on_this_origin(client, base_url, creds):
    """No LAN address, no second site. The Code tab is a path on the same host."""
    r = client.get(f"{base_url}/workshop/api/state")
    assert r.status_code == 200, f"/workshop/ did not serve this user: {r.status_code}"
    assert r.json()["user"] == creds[0], (
        f"the proxy served {r.json().get('user')!r} to {creds[0]} — it is routing on "
        "something other than the authenticated identity"
    )


def test_the_workshop_shell_is_rewritten_for_the_prefix(client, base_url):
    """Its URLs are relative to <base>; serving it under /workshop/ is that one attribute."""
    r = client.get(f"{base_url}/workshop/")
    assert r.status_code == 200
    assert '<base href="/workshop/">' in r.text, (
        "the base was not rewritten, so every relative URL in the shell will escape the "
        "prefix and 404"
    )


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


# ------------------------------------------------------------------ operator console

@pytest.fixture(scope="module")
def operator(base_url) -> httpx.Client:
    user = _secret("enterprise-ai-secrets", "BOOTSTRAP_USER")
    password = _secret("enterprise-ai-secrets", "BOOTSTRAP_PASSWORD")
    c = httpx.Client(verify=False, follow_redirects=True, timeout=60.0)
    landing = c.get(f"{base_url}/portal/")
    m = _FORM.search(landing.text)
    assert m, "no login form"
    c.post(m.group(1).replace("&amp;", "&"), data={"username": user, "password": password})
    return c


def test_a_camper_cannot_see_the_operator_console(client, base_url):
    """404, not 403.

    A non-operator has no business learning that an operator console exists at this path,
    and a 403 says it does.
    """
    for path in ("/portal/api/admin/overview", "/portal/api/admin/audit"):
        r = client.get(f"{base_url}{path}")
        assert r.status_code == 404, f"{path} answered {r.status_code} to a camper"


def test_the_operator_sees_everyone(operator, base_url):
    r = operator.get(f"{base_url}/portal/api/admin/overview")
    assert r.status_code == 200, r.text[:300]
    d = r.json()
    names = {p["username"] for p in d["people"]}
    assert {"baron", "student"} <= names, f"the console is missing people: {names}"
    assert d["totals"]["spend"] >= 0
    # A shared surface key is not a person and must not be listed as one.
    assert "chat-surface" not in names, "the shared chat key was listed as a person"


def test_the_operator_console_is_read_only(operator, base_url):
    """Seeing the bill must not confer the ability to change anything.

    The admin token is still the only thing that can mint, revoke or re-budget; this view
    exists so that reading the numbers does not require holding it.
    """
    for path in ("/portal/api/admin/overview", "/portal/api/admin/audit"):
        r = operator.post(f"{base_url}{path}", json={})
        assert r.status_code in (404, 405), f"{path} accepted a POST ({r.status_code})"
    # And the real admin API stays shut to a browser session.
    r = operator.get(f"{base_url}/admin/keys", follow_redirects=False)
    assert "application/json" not in r.headers.get("content-type", "")


def test_the_operator_console_reports_whether_the_audit_chain_holds(operator, base_url):
    r = operator.get(f"{base_url}/portal/api/admin/audit?limit=5")
    assert r.status_code == 200
    d = r.json()
    assert d["verified"]["ok"] is True, f"the audit chain does not verify: {d['verified']}"
    assert len(d["entries"]) <= 5
    for e in d["entries"]:
        # The window shows what happened, not the evidence. Producing evidence is the
        # export, and that still needs the admin token.
        assert "detail" not in e and "hash" not in e
