"""Drive a real OIDC authorization-code login against the chat surface.

Used by the item-1 test. This performs the same exchange a browser performs — follow the
redirect to the identity provider, submit credentials to its login form, follow the
callback back — so "one login works" is demonstrated rather than asserted from config.
"""

import re

import httpx

# The identity provider uses the bundle's self-signed certificate. Verification is off
# for this client only, because the test's job is to prove the login flow works, not to
# prove trust in a cert we generated ourselves seconds earlier.
_VERIFY = False

_FORM_ACTION = re.compile(
    r'<form[^>]*\bid="kc-form-login"[^>]*\baction="([^"]+)"', re.IGNORECASE | re.DOTALL
)
_FORM_ACTION_FALLBACK = re.compile(
    r'<form[^>]*\baction="([^"]+)"[^>]*\bmethod="post"', re.IGNORECASE | re.DOTALL
)


def _unescape(url: str) -> str:
    return url.replace("&amp;", "&")


def login(chat_url: str, username: str, password: str) -> httpx.Client:
    """Return a client holding an authenticated chat session.

    Raises AssertionError with the failing step if the flow breaks, so a failure names
    where it broke instead of surfacing as a generic 401 at the end.
    """
    client = httpx.Client(verify=_VERIFY, follow_redirects=True, timeout=60.0)

    # 1. Chat surface hands off to the identity provider.
    authorize = client.get(f"{chat_url}/oauth/openid")
    assert authorize.status_code == 200, (
        f"authorize page returned {authorize.status_code}: {authorize.text[:300]}"
    )
    assert "kc-form-login" in authorize.text or "password" in authorize.text, (
        "identity provider did not present a login form; "
        f"landed on {authorize.url}"
    )

    # 2. Submit credentials to the identity provider's login form.
    match = _FORM_ACTION.search(authorize.text) or _FORM_ACTION_FALLBACK.search(authorize.text)
    assert match, "could not find the login form action in the identity provider's page"
    action = _unescape(match.group(1))

    submitted = client.post(
        action,
        data={"username": username, "password": password, "credentialId": ""},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    # 3. Should have been carried back to the chat surface by the callback.
    assert "invalid" not in submitted.text.lower() or "kc-form-login" not in submitted.text, (
        "identity provider rejected the credentials"
    )
    assert str(submitted.url).startswith(chat_url), (
        f"did not land back on the chat surface; ended at {submitted.url}"
    )

    return client


def _cookie_header(client: httpx.Client) -> str:
    """Build a Cookie header by hand, including Secure cookies.

    The chat surface marks its session cookies Secure. Browsers make a standard exception
    and still send Secure cookies to http://localhost, which is why the surface works in
    a browser during local development; http.cookiejar implements no such exception and
    silently drops them, so the session looks empty.

    Emulating the browser here keeps the bundle honest — the alternative is weakening the
    surface's cookie flags to suit the test client. Any deployment not on localhost must
    terminate TLS in front of the chat surface.
    """
    return "; ".join(f"{c.name}={c.value}" for c in client.cookies.jar)


def whoami(client: httpx.Client, chat_url: str) -> dict:
    """The chat surface's view of the authenticated user.

    LibreChat authenticates its API with a bearer token it mints from the refresh cookie
    set at the end of the OIDC callback.
    """
    cookie = _cookie_header(client)
    refreshed = client.post(
        f"{chat_url}/api/auth/refresh", headers={"Cookie": cookie}
    )
    assert refreshed.status_code == 200, (
        f"session refresh failed ({refreshed.status_code}): {refreshed.text[:300]}"
    )
    assert refreshed.headers.get("content-type", "").startswith("application/json"), (
        f"refresh did not return a session: {refreshed.text[:200]}"
    )
    token = refreshed.json().get("token")
    assert token, f"no access token in refresh response: {refreshed.text[:300]}"

    me = client.get(
        f"{chat_url}/api/user", headers={"Authorization": f"Bearer {token}"}
    )
    assert me.status_code == 200, f"/api/user returned {me.status_code}: {me.text[:300]}"
    return me.json()
