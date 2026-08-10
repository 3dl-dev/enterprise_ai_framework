"""The Funnel-fronted origin block must tell Keycloak the true public scheme — enterpriseaiframework-4f1.

WHY THIS EXISTS. Browsers reach this stack over TLS on :8443, but Tailscale Funnel terminates
that TLS and forwards plain HTTP to Caddy's `:8081` block. Caddy therefore reports
`X-Forwarded-Proto: http` to whatever it proxies unless told otherwise. Keycloak, seeing a
"non-secure" request, drops `Secure` (and with it `SameSite=None`) from its SSO cookies. The
account console's redirect-based re-authentication then never gets its session cookie back, so
it bounces through the auth endpoint endlessly — a visible reload loop the moment a user clicks
"Account & password".

Verified live when the fix went in: with `X-Forwarded-Proto: https` forwarded, Keycloak's
`/protocol/openid-connect/auth` sets `AUTH_SESSION_ID=...;Secure;SameSite=None`; without it, the
same request sets `SameSite=Lax` and no `Secure`, and Keycloak logs "Non-secure context
detected". What this file guards is the realistic regression — someone reverting the `:8081`
Keycloak routes to a bare `reverse_proxy`, which silently reintroduces the loop.

The `:8443` LAN block is deliberately NOT required to carry the header: it terminates its own
TLS, so Caddy already reports https there, and browsers never hit it (it is the in-cluster OIDC
backchannel).
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CADDYFILE = REPO / "deploy" / "caddy" / "Caddyfile"


def _block_8081() -> str:
    """The plain-HTTP `:8081` origin listener that Funnel forwards browsers to.

    It opens with a bare `:8081 {` and runs until the next top-level listener, which begins
    with `https://` at column 0. Everything between is this block's body.
    """
    text = CADDYFILE.read_text()
    m = re.search(r"(?m)^:8081 \{\n(.*?)\n(?=^\S)", text, re.DOTALL)
    assert m, "could not locate the :8081 origin block in the Caddyfile"
    return m.group(1)


def _handle_body(block: str, path: str) -> str:
    """The body of a single-level `handle <path> { ... }` — no nested braces inside these."""
    m = re.search(r"handle " + re.escape(path) + r"\s*\{(.*?)\n    \}", block, re.DOTALL)
    assert m, f"no `handle {path}` found in the :8081 block"
    return m.group(1)


def test_keycloak_routes_assert_https_scheme_to_the_pod():
    block = _block_8081()
    for path in ("/realms/*", "/resources/*"):
        body = _handle_body(block, path)
        assert re.search(r"header_up\s+X-Forwarded-Proto\s+https", body), (
            f"the :8081 `handle {path}` must assert `header_up X-Forwarded-Proto https`. "
            "Funnel terminates TLS before this plain-HTTP block, so without it Keycloak sees "
            "http, drops Secure/SameSite=None from its SSO cookies, and the account console "
            "reload-loops (enterpriseaiframework-4f1)."
        )


def test_the_scheme_asserted_is_https_not_http():
    """A copy-paste of the plain block that forwards `http` would be worse than nothing —
    it would pin the broken behaviour. The asserted scheme must be https."""
    block = _block_8081()
    for path in ("/realms/*", "/resources/*"):
        body = _handle_body(block, path)
        forwarded = re.findall(r"header_up\s+X-Forwarded-Proto\s+(\S+)", body)
        assert forwarded == ["https"], (
            f"the :8081 `handle {path}` must forward X-Forwarded-Proto=https, got {forwarded}"
        )
