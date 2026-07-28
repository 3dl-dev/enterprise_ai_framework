"""What the UIs do in an actual browser.

WHY THIS EXISTS

Everything before it was HTTP-level: status codes, bodies, JSON shapes. That proves a
file is served. It proves nothing whatsoever about whether the JavaScript in it runs —
and both surfaces are almost entirely JavaScript. The portal renders every panel from
fetch calls; the Workshop's whole premise is a drawer that opens when a poll says
something appeared. A syntax check and a 200 say nothing about either.

So this drives a real Chromium: signs in, waits for the page to settle, and asserts on
the DOM as rendered. It also fails on any console error or failed network request,
because a page that throws on load can still look fine to curl.
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
    return base64.b64decode(out).decode()


@pytest.fixture(scope="session")
def base_url() -> str:
    return _secret("enterprise-ai-secrets", "PUBLIC_BASE_URL").rstrip("/")


@pytest.fixture(scope="session")
def account() -> tuple[str, str]:
    return (_secret("workspace-user-student", "USERNAME"),
            _secret("workspace-user-student", "PASSWORD"))


@pytest.fixture(scope="session")
def browser():
    os.makedirs(SHOTS, exist_ok=True)
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


# Every context opened by a test, closed after it.
#
# They used to leak. Each one signs in and opens a terminal, and every terminal
# connection spawns its own agent process inside a pod capped at one CPU — so by the end
# of a run a dozen agents were competing, the pod started answering 429, and a terminal
# that normally appears in five seconds had not appeared in thirteen. That looked exactly
# like a product bug and was not one.
_CONTEXTS = []


@pytest.fixture(autouse=True)
def _close_contexts():
    yield
    while _CONTEXTS:
        try:
            _CONTEXTS.pop().close()
        except Exception:
            pass


class Page:
    """A page plus everything that went wrong while it loaded."""

    def __init__(self, page):
        self.page = page
        self.console_errors: list[str] = []
        self.page_errors: list[str] = []
        self.failed_requests: list[str] = []
        page.on("console", self._console)
        page.on("pageerror", lambda e: self.page_errors.append(str(e)))
        page.on("requestfailed", self._failed)

    def _console(self, msg):
        if msg.type == "error":
            self.console_errors.append(msg.text)

    def _failed(self, req):
        # net::ERR_ABORTED is what a cancelled navigation looks like and is not a defect.
        failure = (req.failure or "")
        if "ERR_ABORTED" not in failure:
            self.failed_requests.append(f"{req.url} ({failure})")

    def assert_clean(self, label: str):
        problems = (
            [f"page error: {e}" for e in self.page_errors]
            + [f"console error: {e}" for e in self.console_errors]
            + [f"failed request: {r}" for r in self.failed_requests]
        )
        assert not problems, f"{label} did not load cleanly:\n  " + "\n  ".join(problems)


def _signed_in_portal(browser, base_url, account) -> Page:
    ctx = browser.new_context(ignore_https_errors=True, viewport={"width": 1440, "height": 900})
    _CONTEXTS.append(ctx)
    p = Page(ctx.new_page())
    p.page.goto(f"{base_url}/portal/", wait_until="domcontentloaded", timeout=45000)
    if p.page.locator("#username, input[name='username']").count():
        # Keycloak's login form. Fill it the way a person does.
        if p.page.locator("input[name='username']").count():
            p.page.fill("input[name='username']", account[0])
            p.page.fill("input[name='password']", account[1])
            p.page.click("input[type='submit'], button[type='submit']")
            p.page.wait_for_load_state("load", timeout=45000)
    p.page.wait_for_load_state("load", timeout=45000)
    return p


# ---------------------------------------------------------------- portal

def test_portal_renders_with_no_javascript_errors(browser, base_url, account):
    p = _signed_in_portal(browser, base_url, account)
    p.page.screenshot(path=f"{SHOTS}/portal.png", full_page=True)
    p.assert_clean("portal")


def test_portal_shows_the_signed_in_user(browser, base_url, account):
    p = _signed_in_portal(browser, base_url, account)
    p.page.wait_for_selector("#tab-chat", timeout=20000)
    p.page.click("#avatar-btn")
    p.page.wait_for_selector("#user-menu:not([hidden])", timeout=8000)
    assert p.page.inner_text("#username").strip() == account[0]


def _open_settings(p):
    p.page.wait_for_selector("#tab-chat", timeout=20000)
    p.page.click("#avatar-btn")
    p.page.wait_for_selector("#user-menu:not([hidden])", timeout=8000)
    p.page.click("#mi-settings")
    p.page.wait_for_function("() => document.getElementById('dlg-settings').open", timeout=8000)
    p.page.wait_for_timeout(2500)   # the three panels fetch independently


def test_settings_is_closed_until_asked_for(browser, base_url, account):
    """It is a sheet over the work, not the page you land on.

    Regression: .sheet sets display:flex, which beats the user-agent rule that hides a
    closed <dialog> — so settings rendered permanently on top of the surfaces.
    """
    p = _signed_in_portal(browser, base_url, account)
    p.page.wait_for_selector("#tab-chat", timeout=20000)
    assert p.page.evaluate("() => !document.getElementById('dlg-settings').open")
    assert p.page.locator("#dlg-settings").is_hidden()


def test_portal_panels_actually_populate(browser, base_url, account):
    """The panels are rendered by fetch. If the JS is broken they stay at their placeholders."""
    p = _signed_in_portal(browser, base_url, account)
    _open_settings(p)

    total = p.page.inner_text("#spend-total").strip()
    assert total != "—", "spend never rendered — the placeholder is still there"
    assert total.startswith("$"), f"spend rendered as {total!r}"

    rows = p.page.locator("#spend-rows tr").count()
    assert rows > 0, "spend table has no rows despite the account having spend"

    keys = p.page.locator("#keylist li").count()
    assert keys > 0, "no API keys rendered"


def test_portal_hidden_elements_are_genuinely_hidden(browser, base_url, account):
    """The [hidden] display bug shipped once already; prove it as rendered, not as CSS."""
    p = _signed_in_portal(browser, base_url, account)
    p.page.wait_for_selector("#tab-chat", timeout=20000)
    for sel in ("#failbar", "#toast", "#user-menu"):
        assert p.page.locator(sel).is_hidden(), f"{sel} is visible on a healthy page"


def test_portal_key_rotation_dialog_opens_and_can_be_dismissed(browser, base_url, account):
    """Exercises a native <dialog> and the confirm path WITHOUT actually rotating a key."""
    p = _signed_in_portal(browser, base_url, account)
    _open_settings(p)
    p.page.wait_for_selector("#keylist li", timeout=20000)
    p.page.locator("#keylist li button", has_text="Rotate").first.click()
    p.page.wait_for_selector("#dlg-confirm[open]", timeout=8000)
    assert p.page.locator("#confirm-title").is_visible()
    # Decline. Nothing should be rotated by a test run.
    p.page.locator("#dlg-confirm button[value='cancel']").click()
    # Waiting on a selector would wait for VISIBILITY, and a closed <dialog> is hidden —
    # so the obvious `#dlg-confirm:not([open])` can never match and times out on a dialog
    # that closed correctly. Ask the element itself.
    p.page.wait_for_function(
        "() => !document.getElementById('dlg-confirm').open", timeout=8000)
    p.assert_clean("portal after dialog")


@pytest.mark.parametrize("size", [(1280, 720), (1440, 900), (2560, 1440), (390, 844)])
def test_portal_never_scrolls_sideways(browser, base_url, account, size):
    """The Workshop shipped a header that overflowed below 560px. Check the whole range."""
    w, h = size
    ctx = browser.new_context(ignore_https_errors=True, viewport={"width": w, "height": h})
    _CONTEXTS.append(ctx)
    p = Page(ctx.new_page())
    p.page.goto(f"{base_url}/portal/", wait_until="domcontentloaded", timeout=45000)
    if p.page.locator("input[name='username']").count():
        p.page.fill("input[name='username']", account[0])
        p.page.fill("input[name='password']", account[1])
        p.page.click("input[type='submit'], button[type='submit']")
    p.page.wait_for_load_state("load", timeout=45000)
    p.page.wait_for_selector("#tab-chat", timeout=20000)
    overflow = p.page.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
    p.page.screenshot(path=f"{SHOTS}/portal-{w}x{h}.png", full_page=True)
    assert overflow <= 0, f"page scrolls sideways by {overflow}px at {w}x{h}"


# ---------------------------------------------------------------- workshop

@pytest.fixture(scope="session")
def workshop_url(base_url) -> str:
    """The workshop has no URL of its own any more.

    It used to be a per-user NodePort on a LAN address — a different origin, plain HTTP,
    unroutable from anywhere else, and it did not fail cleanly from another network: it
    hung until the browser gave up. It is now proxied onto this origin so it can be a tab.
    """
    return f"{base_url}/workshop"


def _signed_in_workshop(browser, workshop_url, account) -> Page:
    ctx = browser.new_context(ignore_https_errors=True, viewport={"width": 1440, "height": 900})
    _CONTEXTS.append(ctx)
    p = Page(ctx.new_page())
    p.page.goto(f"{workshop_url}/", wait_until="domcontentloaded", timeout=60000)
    if p.page.locator("input[name='username']").count():
        p.page.fill("input[name='username']", account[0])
        p.page.fill("input[name='password']", account[1])
        p.page.click("input[type='submit'], button[type='submit']")
        p.page.wait_for_load_state("load", timeout=60000)
    p.page.wait_for_load_state("load", timeout=60000)
    return p


def test_workshop_renders_with_no_javascript_errors(browser, workshop_url, account):
    p = _signed_in_workshop(browser, workshop_url, account)
    p.page.screenshot(path=f"{SHOTS}/workshop.png", full_page=True)
    p.assert_clean("workshop")


def _close_drawer(page):
    """Put the layout in a known state.

    The drawer opens by itself once the project has something to show, which is correct
    and is the feature — but it means "at rest" depends on whether an earlier test built
    something. These tests asserted a fixed layout and so passed alone and failed in the
    suite: order-dependence, not a product bug.
    """
    page.evaluate("""() => {
        const el = document.querySelector('[data-drawer]');
        if (el && el.dataset.drawer !== 'closed') document.getElementById('btn-look')?.click();
    }""")
    page.wait_for_timeout(1200)


def test_workshop_terminal_is_the_hero_with_the_drawer_shut(browser, workshop_url, account):
    """The whole point of the rebuild: no preview squatting on half the screen at rest."""
    p = _signed_in_workshop(browser, workshop_url, account)
    p.page.wait_for_timeout(4000)
    _close_drawer(p.page)
    term = p.page.locator("#terminal-frame, iframe[src*='terminal']").first
    assert term.count(), "no terminal iframe on the page at all"
    box = term.bounding_box()
    width = p.page.evaluate("() => document.documentElement.clientWidth")
    assert box and box["width"] > width * 0.75, (
        f"terminal is only {box['width'] if box else 0}px of {width} with the drawer shut"
    )


def _terminal_buffer(page) -> str:
    """Read the terminal's contents.

    NOT from the DOM. ttyd loads the WebGL renderer, which paints to a canvas and creates
    no `.xterm-rows` elements at all — so a DOM query returns nothing on a terminal that
    is working perfectly, and `body.innerText` is empty for the same reason. That false
    negative cost an hour and briefly looked like a camp-blocking outage. The buffer is
    the only honest source.
    """
    frame = next((f for f in page.frames if "/terminal" in (f.url or "")), None)
    if frame is None:
        return ""
    return frame.evaluate("""() => {
        const t = window.term || (window.tty && window.tty.term);
        if (!t) return "";
        const b = t.buffer.active, out = [];
        for (let i = 0; i < b.length; i++) {
            const line = b.getLine(i);
            if (line) { const s = line.translateToString(true); if (s.trim()) out.push(s); }
        }
        return out.join("\\n");
    }""")


def test_the_agent_actually_boots_in_the_terminal(browser, workshop_url, account):
    """End to end: ttyd spawns the shell, the shell starts opencode, opencode finds its
    provider. If the model name is on screen, the whole chain from pod env through the
    config to the gateway catalogue resolved."""
    p = _signed_in_workshop(browser, workshop_url, account)
    buf = ""
    for _ in range(24):          # opencode paints in ~5s; allow for a cold pod
        p.page.wait_for_timeout(2500)
        buf = _terminal_buffer(p.page)
        if "Ask anything" in buf or "GLM" in buf:
            break
    assert buf.strip(), "the terminal never painted anything at all"
    # A fresh project shows the placeholder; one with history shows the resumed
    # conversation instead, because the terminal continues the last session rather than
    # starting a new agent every connection. Both are a booted agent.
    assert ("Ask anything" in buf) or ("ctrl+p commands" in buf), (
        f"opencode never reached a usable prompt. Buffer:\n{buf[:600]}")
    assert "GLM" in buf, (
        "the agent started but shows no model — its provider config did not resolve, "
        f"which is a terminal that cannot spend. Buffer:\n{buf[:600]}"
    )


def _term_dims(page):
    for f in page.frames:
        if f.url and "/workshop/terminal" in f.url:
            return f.evaluate("""() => {
                const t = window.term || (window.tty && window.tty.term);
                if (!t) return null;
                const el = t.element;
                return {cols: t.cols, rows: t.rows,
                        paneH: Math.round(el ? el.clientHeight : 0),
                        cellH: t._core?._renderService?.dimensions?.css?.cell?.height || null};
            }""")
    return None


def test_the_terminal_keeps_its_size_across_a_reconnect(browser, workshop_url, account):
    """ttyd fits once, from whatever the frame measured when its client started.

    On a reconnect — a project switch, New chat, a reload of that frame — it starts before
    the surrounding layout settles, measures a taller box than it ends up with, and reports
    rows it does not have. Measured before the fix: 50 rows in a pane that fits 38, with an
    unchanged viewport. The agent then drew its input box below the visible area.

    Nothing signals it afterwards, because the WINDOW never resized — only the element did.
    """
    p = _signed_in_workshop(browser, workshop_url, account)
    p.page.wait_for_timeout(11000)
    # Fix the layout before measuring: the drawer opens on its own when the project has
    # something to show, and a pane that changes width between the two measurements would
    # make this test fail for a reason that is not the bug it is guarding.
    _close_drawer(p.page)
    p.page.wait_for_timeout(2500)
    first = _term_dims(p.page)
    assert first, "no terminal on first connect"

    p.page.evaluate("() => { document.getElementById('terminal-frame').src = 'terminal/'; }")
    p.page.wait_for_timeout(13000)
    again = _term_dims(p.page)
    assert again, "no terminal after reconnect"

    assert again["rows"] == first["rows"] and again["cols"] == first["cols"], (
        f"the terminal resized itself on reconnect: {first} -> {again}"
    )
    if again["cellH"]:
        used = again["rows"] * again["cellH"]
        assert used <= again["paneH"] + again["cellH"], (
            f"{again['rows']} rows of {again['cellH']}px = {used}px overflows a "
            f"{again['paneH']}px pane — the prompt is drawn off screen"
        )
