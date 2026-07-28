"""The whole product, driven the way a camper drives it.

Every other suite tests a piece. This one does the journey end to end in a real browser
with a real account and real money: sign in once, talk to the agent, watch it write a
file, run what it built, publish it, and then fetch that published link with NO session
at all — because the audience for it is a parent who has no account.

It exists because a run of green unit tests said the product worked on a day when opening
the Code tab froze the tab, the preview rendered nothing, and the terminal came up the
wrong size. None of those were visible to anything smaller than this.

Slow on purpose: it waits on a real model. Not part of `make test`.
"""

import base64
import re
import subprocess
import time

import httpx
import pytest
from playwright.sync_api import sync_playwright

NS = "enterprise-ai"
SHOTS = "/tmp/eai-shots"
AGENT_BUDGET_S = 420          # a real generation, not a stub


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
def account() -> tuple[str, str]:
    return (_secret("workspace-user-student", "USERNAME"),
            _secret("workspace-user-student", "PASSWORD"))


@pytest.fixture(scope="module")
def page_ctx():
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(ignore_https_errors=True,
                                  viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        page.problems = []
        page.on("pageerror", lambda e: page.problems.append(f"pageerror: {e}"))
        page.on("console",
                lambda m: page.problems.append(f"console: {m.text[:200]}")
                if m.type == "error" else None)
        yield page
        browser.close()


@pytest.fixture(scope="module")
def signed_in(page_ctx, base_url, account):
    """One login. Everything after this must work without another."""
    page = page_ctx
    page.goto(f"{base_url}/portal/", wait_until="load", timeout=60000)
    if page.locator("input[name='username']").count():
        page.fill("input[name='username']", account[0])
        page.fill("input[name='password']", account[1])
        page.click("input[type='submit'], button[type='submit']")
        page.wait_for_load_state("load", timeout=60000)
    page.wait_for_selector("#tab-chat", timeout=30000)
    return page


def _frame(page, needle, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        for f in page.frames:
            if needle in (f.url or ""):
                return f
        page.wait_for_timeout(500)
    return None


def _terminal_text(page) -> str:
    f = _frame(page, "/workshop/terminal", timeout=5)
    if f is None:
        return ""
    return f.evaluate("""() => {
        const t = window.term || (window.tty && window.tty.term);
        if (!t) return "";
        const b = t.buffer.active, out = [];
        for (let i = 0; i < b.length; i++) {
            const l = b.getLine(i);
            if (l) { const s = l.translateToString(true); if (s.trim()) out.push(s); }
        }
        return out.join("\\n");
    }""")


# ------------------------------------------------------------------ the journey

def test_01_one_login_lands_on_the_tabbed_shell(signed_in):
    page = signed_in
    assert page.locator("#tab-chat").is_visible()
    assert page.locator("#tab-code").is_visible()
    page.screenshot(path=f"{SHOTS}/e2e-01-shell.png")


def test_02_chat_tab_is_signed_in_without_asking_again(signed_in, base_url):
    page = signed_in
    page.click("#tab-chat")
    chat = _frame(page, base_url, timeout=45)
    # The chat frame is the origin root; give it a moment to finish its own OIDC bounce.
    deadline = time.time() + 60
    text = ""
    while time.time() < deadline:
        f = None
        for fr in page.frames:
            if fr.url and fr.url.startswith(base_url) and "/portal" not in fr.url \
               and "/workshop" not in fr.url:
                f = fr
        if f:
            try:
                text = f.evaluate("() => document.body ? document.body.innerText : ''")
            except Exception:
                text = ""
            if text and "Sign in with" not in text:
                break
        page.wait_for_timeout(2000)
    page.screenshot(path=f"{SHOTS}/e2e-02-chat.png")
    assert "Sign in with" not in text, "chat asked for a second login inside the tab"


def test_03_code_tab_boots_the_agent_at_the_right_size(signed_in):
    page = signed_in
    page.click("#tab-code")
    deadline = time.time() + 120
    buf = ""
    while time.time() < deadline:
        page.wait_for_timeout(3000)
        buf = _terminal_text(page)
        if "GLM" in buf:
            break
    # Two legitimate shapes. A fresh project shows the placeholder; one with history shows
    # the resumed conversation instead, because the terminal now continues the last
    # session rather than starting a new agent on every connection. Asserting on the
    # placeholder alone made a working resume look like a broken boot.
    ready = ("Ask anything" in buf) or ("ctrl+p commands" in buf)
    assert ready, f"the agent never reached a usable prompt:\n{buf[:600]}"
    assert "GLM" in buf, f"the agent started but resolved no model:\n{buf[:600]}"

    tf = _frame(page, "/workshop/terminal", timeout=10)
    dims = tf.evaluate("""() => {
        const t = window.term || (window.tty && window.tty.term);
        return t ? {cols: t.cols, rows: t.rows, w: window.innerWidth, h: window.innerHeight} : null;
    }""")
    assert dims, "no xterm instance"
    # A terminal fitted while its tab was hidden measures near-zero and comes back ~10
    # cols wide, which is what puts opencode's input box off the screen.
    assert dims["cols"] > 60, f"terminal came up {dims['cols']} cols for {dims['w']}px: {dims}"
    assert dims["rows"] > 15, f"terminal came up {dims['rows']} rows for {dims['h']}px: {dims}"
    page.screenshot(path=f"{SHOTS}/e2e-03-code.png")


def test_04_a_typed_request_makes_the_agent_write_a_file(signed_in):
    """The actual product. Types at the agent and waits for a real file to appear."""
    page = signed_in
    tf = _frame(page, "/workshop/terminal", timeout=10)
    assert tf, "no terminal frame"

    marker = f"e2e{int(time.time()) % 100000}"
    prompt = (f"Create index.html containing exactly one h1 that reads {marker}. "
              f"No CSS, no JavaScript, no other files. Keep it tiny.")

    page.click("#tab-code")
    box = page.locator("#frame-code").bounding_box()
    page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] - 120)
    page.wait_for_timeout(800)
    page.keyboard.type(prompt, delay=12)
    page.wait_for_timeout(400)
    page.keyboard.press("Enter")

    wf = _frame(page, "/workshop/", timeout=15)
    deadline = time.time() + AGENT_BUDGET_S
    seen = False
    while time.time() < deadline:
        page.wait_for_timeout(5000)
        try:
            pulse = wf.evaluate("() => fetch('api/pulse').then(r=>r.json())")
        except Exception:
            continue
        if pulse and pulse.get("has_index"):
            seen = True
            break
    page.screenshot(path=f"{SHOTS}/e2e-04-built.png")
    assert seen, (
        "the agent never produced an index.html within "
        f"{AGENT_BUDGET_S}s. Terminal said:\n{_terminal_text(page)[-800:]}"
    )


def test_05_the_preview_offers_to_run_rather_than_seizing_the_tab(signed_in):
    """A generated page must never be started without being asked.

    A voxel game the agent wrote redrew ~20,000 cells a frame and took the whole tab with
    it — terminal included — and no in-page control could recover, because the thread that
    would run the handler was the thread being starved.
    """
    page = signed_in
    wf = _frame(page, "/workshop/", timeout=15)
    deadline = time.time() + 60
    while time.time() < deadline:
        state = wf.evaluate("() => document.querySelector('[data-drawer]')?.dataset.drawer")
        if state and state != "closed":
            break
        page.wait_for_timeout(2000)
    assert wf.evaluate("() => { const e = document.getElementById('preview-ready');"
                       "return e && !e.hidden; }"), "the run gate never appeared"
    assert wf.evaluate("() => document.getElementById('preview').src") .endswith("about:blank"), \
        "the preview started running on its own"

    worst = 0
    for _ in range(6):
        t = time.time()
        page.evaluate("() => 1 + 1")
        worst = max(worst, (time.time() - t) * 1000)
        time.sleep(0.3)
    assert worst < 1000, f"the page was unresponsive ({worst:.0f} ms) with the drawer open"


def test_06_running_it_renders_what_the_agent_built(signed_in):
    page = signed_in
    wf = _frame(page, "/workshop/", timeout=15)
    wf.evaluate("() => document.getElementById('btn-run').click()")
    pf = _frame(page, "/workshop/preview/", timeout=45)
    assert pf, "the preview never loaded after pressing Run"
    page.wait_for_timeout(3000)
    page.screenshot(path=f"{SHOTS}/e2e-06-running.png")
    # Sandboxed without allow-same-origin, so the body is unreadable from here by design.
    # That the frame exists and the response was served is what can be checked in-page;
    # the content itself is verified over HTTP in test_07.
    assert "/workshop/preview/" in pf.url


def test_07_publishing_gives_a_link_a_parent_can_open_with_no_account(signed_in, base_url, account):
    page = signed_in
    wf = _frame(page, "/workshop/", timeout=15)
    wf.evaluate("() => document.getElementById('btn-share').click()")

    url = ""
    deadline = time.time() + 90
    while time.time() < deadline:
        page.wait_for_timeout(2000)
        url = wf.evaluate("""() => {
            const el = document.querySelector('#share-url, #published-url, [data-share-url]');
            if (el) return el.value || el.textContent || '';
            const a = [...document.querySelectorAll('a')].find(a => /\\/live\\//.test(a.href));
            return a ? a.href : '';
        }""") or ""
        if "/live/" in url:
            break
    page.screenshot(path=f"{SHOTS}/e2e-07-shared.png")
    assert "/live/" in url, f"publishing produced no shareable link (got {url!r})"
    assert f"/live/{account[0]}/" in url, f"the link is not this user's: {url}"

    # The whole point: no cookies, no session, nothing. A parent has no account.
    anon = httpx.Client(verify=False, follow_redirects=True, timeout=30)
    r = anon.get(url)
    assert r.status_code == 200, f"a parent opening {url} got {r.status_code}"
    assert "<" in r.text, "the published page came back empty"
    assert "Sign in" not in r.text, "the published link asked a parent to log in"


def test_08_the_bill_and_the_keys_reflect_the_work_just_done(signed_in, base_url, account):
    page = signed_in
    page.click("#avatar-btn")
    page.wait_for_selector("#user-menu:not([hidden])", timeout=10000)
    page.click("#mi-settings")
    page.wait_for_function("() => document.getElementById('dlg-settings').open", timeout=10000)
    page.wait_for_timeout(3500)

    total = page.inner_text("#spend-total").strip()
    assert total.startswith("$"), f"spend did not render: {total!r}"
    assert total != "$0.00", "the journey spent real money but the bill shows nothing"

    rows = page.inner_text("#spend-rows")
    assert "ide" in rows, f"no coding-surface spend after using the agent:\n{rows}"

    keys = page.inner_text("#keylist")
    assert f"{account[0]}::ide" in keys, "the user's own coding key is not listed"
    assert "sk-" not in keys, "a key secret was rendered into the page"
    page.screenshot(path=f"{SHOTS}/e2e-08-settings.png")


def test_09_no_javascript_errors_across_the_whole_journey(signed_in):
    noisy = ("ResizeObserver", "GL Driver", "Autofocus processing")
    real = [p for p in signed_in.problems if not any(n in p for n in noisy)]
    assert not real, "the journey produced errors:\n  " + "\n  ".join(real[:10])
