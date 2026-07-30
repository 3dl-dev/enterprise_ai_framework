"""enterpriseaiframework-40f: a real browser, on the tailnet, signing in to CHAT.

WHY A BROWSER AND NOT curl

The defect is a cookie policy: LibreChat's OIDC session cookie was marked Secure while the
request the chat pod actually sees (behind Caddy's plain-HTTP :8081 block, the one Tailscale
Funnel forwards to) reports as plain HTTP, so express-session's own gate
(`cookie.secure && !issecure(req, trustProxy)`) silently drops the Set-Cookie header outright.
curl can show what the SERVER sent; only a real browser's cookie jar shows what was actually
STORED, and a Secure-flag mismatch is exactly the kind of bug that is invisible to curl and
would look "fixed" to any test that only inspects response headers.

This drives Chromium against the live cluster's real public origin — the same one a second
machine on the tailnet uses — through the real Keycloak login form, then asserts on the
browser's own cookie jar and on a page reload, not on anything curl could have told us.
"""

import base64
import os
import subprocess

import pytest
from playwright.sync_api import sync_playwright

NS = "enterprise-ai"
SHOTS = os.environ.get("BROWSER_SHOT_DIR", "/tmp/eai-shots")


def _secret(name: str, key: str) -> str:
    out = subprocess.run(
        ["kubectl", "-n", NS, "get", "secret", name, "-o", f"jsonpath={{.data.{key}}}"],
        capture_output=True, text=True, timeout=60, check=True,
    ).stdout
    if not out:
        pytest.fail(f"secret {name}/{key} is empty or missing")
    return base64.b64decode(out).decode()


@pytest.fixture(scope="session")
def base_url() -> str:
    return _secret("enterprise-ai-secrets", "PUBLIC_BASE_URL").rstrip("/")


@pytest.fixture(scope="session")
def account() -> tuple[str, str]:
    # The founder's own bootstrap account — this item is "he cannot sign in", not a fixture user.
    return (_secret("enterprise-ai-secrets", "BOOTSTRAP_USER"),
            _secret("enterprise-ai-secrets", "BOOTSTRAP_PASSWORD"))


@pytest.fixture(scope="session")
def browser():
    os.makedirs(SHOTS, exist_ok=True)
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


def _sign_in_to_chat(browser, base_url, account):
    """A fresh context — no prior cookies, no prior Keycloak SSO session — driven exactly the
    way a person on a second machine would use it: open the origin, land on Keycloak (chat's
    OPENID_AUTO_REDIRECT skips its own login page), type the password, land back in chat."""
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    page = ctx.new_page()
    console_errors = []
    page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)

    page.goto(base_url + "/", wait_until="domcontentloaded", timeout=45000)
    # OPENID_AUTO_REDIRECT=true: chat bounces straight to Keycloak, no button to click.
    page.wait_for_selector("input[name='username']", timeout=20000)
    page.fill("input[name='username']", account[0])
    page.fill("input[name='password']", account[1])
    page.click("input[type='submit'], button[type='submit']")
    page.wait_for_load_state("load", timeout=45000)
    return ctx, page, console_errors


def test_second_machine_signs_in_to_chat_and_the_session_survives_a_reload(
        browser, base_url, account):
    ctx, page, console_errors = _sign_in_to_chat(browser, base_url, account)
    try:
        # 1. The login actually completed: we are back on the chat origin (the SPA's router
        #    lands a fresh login on /c/new, a new conversation — not bare "/"), not still on
        #    Keycloak and not bounced to /login?error=auth_failed (the failure mode this item
        #    is about — "Unable to verify authorization request state").
        for _ in range(20):
            if page.url.startswith(base_url) and "/realms/" not in page.url:
                break
            page.wait_for_timeout(1000)
        assert page.url.startswith(base_url) and "/realms/" not in page.url, (
            f"never landed back on the chat origin; stuck at {page.url}"
        )
        assert "error=" not in page.url, f"landed back with an error in the URL: {page.url}"
        assert "/login" not in page.url, f"bounced to the login page instead: {page.url}"

        page.wait_for_timeout(2000)
        page.screenshot(path=f"{SHOTS}/chat-signed-in.png", full_page=True)

        # 2. What the item demands directly: the cookie was actually STORED by the browser,
        #    not merely sent. This is the assertion curl cannot make — it has no cookie jar
        #    that enforces a Secure policy the way a real browser does.
        cookies = {c["name"]: c for c in ctx.cookies()}
        assert "refreshToken" in cookies, (
            "no refreshToken cookie in the browser's jar after login — the browser refused "
            f"to store it (or it was never sent). Jar has: {sorted(cookies)}"
        )
        refresh = cookies["refreshToken"]

        # 3. The flags a real login cookie needs, asserted on the STORED cookie (Playwright's
        #    ctx.cookies() reflects what Chromium actually kept), not on a Set-Cookie header:
        #    HttpOnly so page JS cannot read it, SameSite so it is not replayable cross-site.
        assert refresh["httpOnly"] is True, f"refreshToken not HttpOnly: {refresh}"
        assert refresh["sameSite"] in ("Strict", "Lax"), f"refreshToken sameSite: {refresh}"

        # 4. The point of the whole item: reload, and still be signed in. If the cookie had
        #    been dropped (the original bug) or rejected by the browser, a reload bounces
        #    straight back to Keycloak.
        signed_in_url = page.url
        page.reload(wait_until="load", timeout=45000)
        page.wait_for_timeout(2000)
        assert page.url.startswith(base_url) and "/realms/" not in page.url, (
            f"reload bounced off the chat origin to {page.url} (was {signed_in_url}) — "
            "i.e. the session did not survive the reload"
        )
        assert not page.locator("input[name='username']").count(), (
            "a Keycloak login form is back on screen after reload — the session did not persist"
        )
        page.screenshot(path=f"{SHOTS}/chat-after-reload.png", full_page=True)

        # The cookie must still be there after the reload too — not just immediately post-login.
        cookies_after = {c["name"]: c for c in ctx.cookies()}
        assert "refreshToken" in cookies_after, "refreshToken cookie gone after reload"

        assert not console_errors, f"console errors during sign-in: {console_errors}"
    finally:
        ctx.close()
