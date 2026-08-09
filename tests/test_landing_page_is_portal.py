"""The bare landing page is the portal wrapper, never raw LibreChat — enterpriseaiframework-6c9.

The signed-in front door is the portal (`/portal/`): the Chat and Code tabs, spend, keys and
published work, the closest thing this stack has to claude.ai. Someone opening the origin
root should land there and switch into coding mode or check their usage from it — not drop
straight into a bare LibreChat with no way back to the rest of the platform.

WHY THE GUARD IS LOAD-BEARING, NOT POLISH. The redirect cannot be unconditional. The portal
embeds chat in an `<iframe>` whose `src` is this same origin root
(`control-plane/app/portal_static/app.js`), and LibreChat's own post-login flow redirects
back to `/` inside that frame. A request loading a document *into a frame* carries
`Sec-Fetch-Dest: iframe`; a top-level browser navigation carries `document`. So the Caddy
redirect fires only when the header is NOT `iframe` — otherwise the Chat tab would load the
portal inside itself and the wrapper would nest recursively.

These are static checks on the versioned gateway-VM config (`deploy/caddy/Caddyfile`). The
behaviour itself was verified live when the fix went in: `Sec-Fetch-Dest: document` → 302
`/portal/`, `Sec-Fetch-Dest: iframe` → 200 chat, and `/oauth/openid`, `/c/new`, `/realms/*`
all unchanged. What this file guards is the realistic regression — someone editing the
Caddyfile back to a plain catch-all, or changing the iframe's src, without realising the two
depend on each other.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CADDYFILE = REPO / "deploy" / "caddy" / "Caddyfile"
PORTAL_JS = REPO / "control-plane" / "app" / "portal_static" / "app.js"
PORTAL_PY = REPO / "control-plane" / "app" / "portal.py"

# Both origin listeners must carry the redirect: :8081 is what Funnel forwards browsers to,
# and the :8443 LAN block mirrors it. `handle /` has no nested braces, so a non-greedy match
# is exact.
ROOT_HANDLE_RE = re.compile(r"handle /\s*\{(.*?)\}", re.DOTALL)


def _root_handle_blocks() -> list[str]:
    return ROOT_HANDLE_RE.findall(CADDYFILE.read_text())


def test_caddyfile_exists_and_is_the_deployed_artifact():
    assert CADDYFILE.exists(), (
        "deploy/caddy/Caddyfile is missing — the gateway-VM edge config must be versioned, or "
        "a VM rebuild silently drops the routing (that is how the login outage went unnoticed)"
    )


def test_both_origin_blocks_redirect_the_bare_root_to_the_portal():
    blocks = _root_handle_blocks()
    assert len(blocks) == 2, (
        f"expected a `handle /` in both the :8081 and :8443 origin blocks, found {len(blocks)}"
    )
    for block in blocks:
        assert "/portal/" in block and "redir" in block, (
            "each `handle /` must redirect the bare root to /portal/ (the wrapper), so nobody "
            "lands on raw LibreChat"
        )


def test_the_redirect_is_guarded_against_the_chat_iframe():
    for block in _root_handle_blocks():
        assert "Sec-Fetch-Dest" in block and "iframe" in block, (
            "the root redirect must be guarded by Sec-Fetch-Dest so the portal's Chat iframe "
            "(and LibreChat's post-login redirect back to / inside it) is NOT redirected — "
            "without this the wrapper nests itself recursively"
        )
        assert "reverse_proxy" in block, (
            "the iframe case must still fall through to the chat surface, or the Chat tab breaks"
        )


def test_the_redirect_only_matches_the_exact_root():
    """`handle /` matches exactly `/`; a `handle /*` here would swallow chat's /api, /assets,
    /oauth and /c/... into the redirect and break the whole surface."""
    text = CADDYFILE.read_text()
    assert "handle /*" not in text, (
        "the landing-page redirect must match the exact root, not a /* prefix"
    )


def test_the_chat_iframe_still_loads_the_origin_root():
    """This is the coupling that makes the Sec-Fetch-Dest guard necessary. If the Chat tab
    stops loading the origin root, someone may 'simplify' the guard away — this pins why it
    exists."""
    js = PORTAL_JS.read_text()
    assert 'LINKS.chat || "/"' in js, (
        "the portal's Chat iframe loads the origin root (LINKS.chat || '/'); if this changed, "
        "revisit the Caddy root redirect and its Sec-Fetch-Dest guard together"
    )
    py = PORTAL_PY.read_text()
    assert 'PUBLIC_BASE_URL or "/"' in py, (
        "portal.py serves the Chat link as the origin root; keep it and the Caddy redirect in "
        "sync"
    )
