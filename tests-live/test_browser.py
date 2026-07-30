"""What the UIs do in an actual browser.

WHY THIS EXISTS

Everything before it was HTTP-level: status codes, bodies, JSON shapes. That proves a
file is served. It proves nothing whatsoever about whether the JavaScript in it runs —
and both surfaces are almost entirely JavaScript. The portal renders every panel from
fetch calls; the Workshop's whole premise is a drawer that opens when a poll says
something appeared. A syntax check and a 200 say nothing about either.

So this drives a real Chromium, waits for the page to settle, and asserts on the DOM as
rendered. It also fails on any console error or failed network request, because a page that
throws on load can still look fine to curl.

WHERE IT DRIVES THAT BROWSER, AND WHY THAT CHANGED (enterpriseaiframework-cf5)

It used to sign in to the live cluster, as `student`, out of secret/workspace-user-student.
`student` is a person. Two of the tests below open the Code tab, and the Code tab drives
that person's OWN pod: switching to it starts a session, rewrites
`.meta/<project>.session` and clears the `.new-session` flag. A measured run changed both.
So `make test-browser` could not be run while anybody was signed in, which in practice
meant it could not be run — and items whose done-condition needed it were blocked on a
maintenance window.

Almost none of it needed a person. It needed A portal, A workshop, and a browser. Those are
hosted on loopback by portal_harness.py, which serves the shipped page, the shipped
`require_user`, the shipped settings handlers and the real `shell-server.py` over a
throwaway projects root, and mints its own per-process token instead of reading the shared
per-deployment one. Ten of the twelve tests here run against that and take no identity from
anywhere; they are safe at any hour.

The two that are left need a real ttyd, a real xterm.js and a real opencode resolving a
real model through the gateway. Nothing on loopback can stand in for that without the test
becoming a test of the stand-in. They are marked `needs_real_user`, they resolve their
account through tests-live/live_identity.py — which refuses anything not explicitly named
AND marked throwaway — and conftest.py deselects them unless that account is named. See
live_identity.py for why the env var is not the whole gate.

WHAT IS NOT WEAKER THAN IT WAS. Every assertion the live versions made is still made, and
the panel and rotation tests make strictly stronger ones, because a fixture can contain a
second account's spend row, a second account's key, and a key secret — none of which a live
deployment's own data can be relied on to provide. "A row exists" was what the live test
could check. "This row is right, and that row is absent" is what these check.
"""

import os
import re

import pytest
from playwright.sync_api import expect, sync_playwright

import live_identity
import portal_harness

SHOTS = os.environ.get("BROWSER_SHOT_DIR", "/tmp/eai-shots")


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


# ---------------------------------------------------------------- the hosted stack
#
# The portal, its workshop proxy, the shipped settings handlers and a throwaway workshop,
# all on loopback. No cluster, no credential, no pod anybody owns.


@pytest.fixture(scope="module")
def hosted():
    s = portal_harness.stack()
    yield s
    portal_harness.shutdown()


@pytest.fixture()
def hosted_workshop(hosted, tmp_path):
    hosted.start_shell(tmp_path / "projects")
    yield hosted
    hosted.stop_shell()


@pytest.fixture()
def hosted_no_workshop(hosted):
    """The same stack with nothing serving the workshop port. Torn down either way, so a
    stand-in left listening cannot leak into another test."""
    assert not hosted.shell_running
    yield hosted
    hosted.stop_shell()


def _open(browser, hosted, path="/portal/", viewport=None) -> tuple[Page, list]:
    """A page on the hosted origin, plus every response status it saw.

    The statuses are collected because `requestfailed` does not fire on an HTTP error
    response — it is for transport failures — so a framed surface answering 401 or 500 is
    not a "failed request" as far as Playwright is concerned. Recording the response is how
    a test sees the status a frame actually got.
    """
    ctx = browser.new_context(viewport=viewport or {"width": 1440, "height": 900})
    _CONTEXTS.append(ctx)
    p = Page(ctx.new_page())
    seen: list[tuple[str, int]] = []
    p.page.on("response", lambda r: seen.append((r.url, r.status)))
    p.page.goto(f"{hosted.base_url}{path}", wait_until="domcontentloaded", timeout=45000)
    return p, seen


def _hosted_portal(browser, hosted) -> tuple[Page, list]:
    p, seen = _open(browser, hosted)
    p.page.wait_for_selector("#tab-chat", timeout=20000)
    return p, seen


# ---------------------------------------------------------------- portal

def test_portal_renders_with_no_javascript_errors(browser, hosted):
    p, _ = _hosted_portal(browser, hosted)
    p.page.screenshot(path=f"{SHOTS}/portal.png", full_page=True)
    p.assert_clean("portal")


def test_portal_shows_the_signed_in_user(browser, hosted):
    """The name in the menu comes from the shipped `me` handler, not from the page.

    Asserted against the identity the harness's oauth2-proxy hop injected, so a page that
    rendered a hardcoded string, an empty string, or the wrong field would fail. The avatar
    and the email are checked too: all three are set from the same payload by `loadMe`, and
    the reason to have this test is that one of them once was not.

    Deliberately NOT asserted: `document.title`. `loadMe` sets it to the username and then
    `mountFrames` immediately overwrites it with the tab name, so the username is never in
    the title by the time anything can look. Measured, not assumed — the assertion was
    written, run, and failed. It is a dead line in app.js, not a defect worth an item, and
    pinning current behaviour here would be pinning the wrong thing either way.
    """
    p, _ = _hosted_portal(browser, hosted)
    p.page.click("#avatar-btn")
    p.page.wait_for_selector("#user-menu:not([hidden])", timeout=8000)
    assert p.page.inner_text("#username").strip() == portal_harness.USER
    assert p.page.inner_text("#avatar").strip() == portal_harness.USER[0].upper()
    assert p.page.inner_text("#email").strip() == f"{portal_harness.USER}@example.invalid"
    # And not somebody else's, which is the half a presence check cannot see.
    assert portal_harness.OTHER_USER not in p.page.inner_text("body")


def test_settings_is_closed_until_asked_for(browser, hosted):
    """It is a sheet over the work, not the page you land on.

    Regression: .sheet sets display:flex, which beats the user-agent rule that hides a
    closed <dialog> — so settings rendered permanently on top of the surfaces.
    """
    p, _ = _hosted_portal(browser, hosted)
    assert p.page.evaluate("() => !document.getElementById('dlg-settings').open")
    assert p.page.locator("#dlg-settings").is_hidden()


def _open_settings(p):
    p.page.wait_for_selector("#tab-chat", timeout=20000)
    p.page.click("#avatar-btn")
    p.page.wait_for_selector("#user-menu:not([hidden])", timeout=8000)
    p.page.click("#mi-settings")
    p.page.wait_for_function("() => document.getElementById('dlg-settings').open", timeout=8000)
    # The three panels fetch independently. Waited on by their rendered result rather than
    # by a sleep, so this cannot pass by having timed out into an empty table.
    p.page.wait_for_function(
        "() => document.getElementById('spend-total').textContent.trim() !== '—'",
        timeout=20000)
    p.page.wait_for_selector("#keylist li", timeout=20000)


def test_portal_panels_actually_populate(browser, hosted):
    """The panels are rendered by fetch. If the JS is broken they stay at their placeholders.

    That was the whole of this test when it ran against the cluster: a total that is not the
    placeholder, `rows > 0`, `keys > 0`. Those are presence checks, and a presence check
    cannot tell a correct bill from a bill that has somebody else's spend in it — which is
    the defect class this repo keeps producing. Against the harness the upstream data is
    known, and it deliberately contains a second account's row and key, a chat row keyed by
    a LibreChat ObjectId, and a key carrying a secret. So the assertions here are what the
    numbers ARE, and what must not be among them.
    """
    p, _ = _hosted_portal(browser, hosted)
    _open_settings(p)

    # 1. The total. Not "not the placeholder" — the exact figure the shipped handler must
    #    compute from the fixture rows, rendered by the shipped money().
    total = p.page.inner_text("#spend-total").strip()
    assert total == portal_harness.SPEND_TOTAL_RENDERED, (
        f"spend rendered as {total!r}; the three rows belonging to this user sum to "
        f"{portal_harness.SPEND_TOTAL_RENDERED}"
    )

    # 2. Every surface, in the order the handler sorts them, and no others. The chat row is
    #    the one that matters: its principal in the ledger is a 24-hex ObjectId, so a
    #    regression that filtered before translating would drop it silently — and a missing
    #    number reads as a zero, not as an error.
    surfaces = p.page.locator("#spend-rows tr .surface-tag").all_inner_texts()
    assert [s.strip() for s in surfaces] == portal_harness.SPEND_SURFACES_RENDERED, (
        f"spend rows are {surfaces}; expected {portal_harness.SPEND_SURFACES_RENDERED}"
    )

    sheet = p.page.inner_text("#dlg-settings")

    # 3. The other account's row is much larger than everything here. It must be nowhere.
    assert portal_harness.OTHER_USER not in sheet, (
        "another account's name is rendered in this user's settings sheet"
    )
    assert "7.5" not in sheet and "$7.50" not in sheet, (
        f"another account's spend leaked into this user's bill:\n{sheet}"
    )
    # And the untranslated chat id must not be what a person is shown.
    assert portal_harness.CHAT_ID not in sheet, (
        "the chat row rendered as a raw LibreChat ObjectId instead of a surface name"
    )

    # 4. The keys: this user's two, in the handler's order, and not the other account's,
    #    and not the shared chat-surface key, which is not a person.
    aliases = [t.strip() for t in p.page.locator("#keylist .alias").all_inner_texts()]
    assert aliases == portal_harness.KEY_ALIASES_RENDERED, (
        f"key list is {aliases}; expected {portal_harness.KEY_ALIASES_RENDERED}"
    )
    assert "chat-surface" not in sheet, "the shared chat key was listed as this user's"

    # 5. No secret. One of the fixture keys carries a `token`, so the shipped projection
    #    dropping it is a real result rather than a vacuous one.
    assert "sk-" not in sheet, f"a key secret was rendered into the page:\n{sheet}"

    # 6. Published work, from the listing the shipped handler fetched over HTTP. The file
    #    entry in that listing must not become a project.
    work = p.page.inner_text("#worklist")
    assert portal_harness.PUBLISHED_PROJECT in work, f"published work did not render: {work!r}"
    assert "index.html" not in work, "a file in the listing was rendered as a project"

    # 7. This user is not an operator, so the console must not be there at all. The panel
    #    is the unchanged path: it has nothing to do with the assertions above and would
    #    be the first thing a broken is_admin check exposed.
    assert p.page.locator("#panel-admin").is_hidden(), (
        "the operator console rendered for a non-operator"
    )

    p.page.screenshot(path=f"{SHOTS}/portal-settings.png")
    p.assert_clean("portal settings sheet")


def test_portal_hidden_elements_are_genuinely_hidden(browser, hosted):
    """The [hidden] display bug shipped once already; prove it as rendered, not as CSS."""
    p, _ = _hosted_portal(browser, hosted)
    for sel in ("#failbar", "#toast", "#user-menu"):
        assert p.page.locator(sel).is_hidden(), f"{sel} is visible on a healthy page"


def test_portal_key_rotation_dialog_opens_and_can_be_dismissed(browser, hosted):
    """Exercises a native <dialog> and the confirm path, and proves nothing was rotated.

    The live version of this test said "Decline. Nothing should be rotated by a test run."
    in a comment and had no way to check it: the only evidence available was the key list,
    which looks identical after a rotation. The harness records every call the page makes to
    the rotate endpoint, so declining is now a claim with a check behind it.
    """
    p, _ = _hosted_portal(browser, hosted)
    before = len(hosted.rotations)
    _open_settings(p)

    row = p.page.locator("#keylist li").first
    alias = row.locator(".alias").inner_text().strip()
    row.locator("button", has_text="Rotate").click()
    p.page.wait_for_selector("#dlg-confirm[open]", timeout=8000)
    assert p.page.locator("#confirm-title").is_visible()
    # Which key it is about. A confirm dialog wired to the wrong row is a dialog that
    # rotates somebody's working key when they say yes, and "a dialog opened" cannot see it.
    assert alias in p.page.inner_text("#confirm-title"), (
        f"the confirm dialog says {p.page.inner_text('#confirm-title')!r} for row {alias!r}"
    )

    # Decline. Nothing should be rotated by a test run.
    p.page.locator("#dlg-confirm button[value='cancel']").click()
    # Waiting on a selector would wait for VISIBILITY, and a closed <dialog> is hidden —
    # so the obvious `#dlg-confirm:not([open])` can never match and times out on a dialog
    # that closed correctly. Ask the element itself.
    p.page.wait_for_function(
        "() => !document.getElementById('dlg-confirm').open", timeout=8000)
    # The new-key dialog is what appears when a rotation succeeds; it must not.
    assert p.page.evaluate("() => !document.getElementById('dlg-key').open")
    assert hosted.rotations[before:] == [], (
        f"declining still rotated a key: {hosted.rotations[before:]}"
    )
    p.assert_clean("portal after dialog")


@pytest.mark.parametrize("size", [(1280, 720), (1440, 900), (2560, 1440), (390, 844)])
def test_portal_never_scrolls_sideways(browser, hosted, size):
    """The Workshop shipped a header that overflowed below 560px. Check the whole range."""
    w, h = size
    p, _ = _open(browser, hosted, viewport={"width": w, "height": h})
    p.page.wait_for_selector("#tab-chat", timeout=20000)
    overflow = p.page.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
    p.page.screenshot(path=f"{SHOTS}/portal-{w}x{h}.png", full_page=True)
    assert overflow <= 0, f"page scrolls sideways by {overflow}px at {w}x{h}"


# ------------------------------------------- the route back to the portal, self-hosted


def _chrome_is_not_covered(p):
    """The check the first version of this test consisted of, kept and labelled.

    It measures `#frame-code`'s rectangle IN THE PARENT DOCUMENT, which is a property of
    the portal's own layout and says nothing whatever about what is inside the frame. It
    passes identically when the workshop answers 200 and when it answers 502 — proven by
    the two tests below, which both call it. It is a necessary condition and never
    sufficient, and it is separated out here so that nobody mistakes it for the evidence.
    """
    header = p.page.locator("header.bar").bounding_box()
    frame = p.page.locator("#frame-code").bounding_box()
    assert header and header["height"] > 0, "the portal header has no box at all"
    assert frame, "the Code tab has no iframe box"
    assert header["y"] + header["height"] <= frame["y"], (
        f"the workshop iframe ({frame}) starts above where the header ends ({header}) — "
        "the route back to the portal would be covered up"
    )
    return header, frame


def _menu_from_the_code_tab(p, hosted):
    """Open the account menu while the Code tab is the visible one, and USE it.

    Reading hrefs is not enough: both of these carry target=_blank and are populated from
    the links payload, so any non-empty string satisfies a presence check — including a
    stale one or one naming another tenant's realm. The harness serves the account console
    and the published site at the one path each that the shipped code is supposed to emit,
    so following the link is what distinguishes right from merely present.
    """
    p.page.click("#avatar-btn")
    p.page.wait_for_selector("#user-menu:not([hidden])", timeout=8000)
    assert p.page.inner_text("#username").strip() == portal_harness.USER, (
        "the account menu opened from the Code tab does not name the signed-in user")

    for item, want_url, marker in (
        # The account item is wired to links.password, i.e. the console's security
        # section — asserted with the fragment, because landing on the console root would
        # mean the more specific destination had been lost.
        ("#mi-account", hosted.password_url, "#kc-account"),
        ("#mi-published", hosted.published_url, "#published-stub"),
    ):
        with p.page.context.expect_page() as popup:
            p.page.click(item)
        opened = popup.value
        opened.wait_for_load_state("load", timeout=20000)
        assert opened.url == want_url, (
            f"{item} reached {opened.url!r} from the Code tab; the portal is supposed to "
            f"send a user to {want_url!r}"
        )
        expect(opened.locator(marker)).to_be_visible(timeout=10000)
        opened.close()

    # Not followed: following it ends the session, and the rest of the test needs it. Its
    # resolved target is asserted instead — a relative href that resolved against the
    # wrong document would show up here.
    assert p.page.locator("#signout").evaluate("el => el.href") == \
        f"{hosted.base_url}/portal/oauth2/sign_out"


def test_the_portal_is_reachable_from_inside_the_workshop_tab(browser, hosted_workshop):
    """The case the item called most likely to be missed.

    The portal is the front door — spend, keys, published work, the account console — and
    the Code tab is the surface a user spends the day inside. Chat and Code used to be
    separate sites with no route back; they are now iframes on one page, and the claim
    under test is that the portal's own chrome stays outside both of them and stays usable
    while the workshop (which itself nests a further terminal iframe) is what is on screen.
    """
    p, seen = _hosted_portal(browser, hosted_workshop)

    # The tab that was already working, asserted too: this test must fail if the Chat
    # surface stops being framed, not only if the Code one does.
    expect(p.page.frame_locator("#frame-chat").locator("#chat-stub")).to_be_visible(
        timeout=20000)

    p.page.click("#tab-code")
    code = p.page.frame_locator("#frame-code")

    # The load condition, and it is a real one rather than a sleep: this text is written by
    # the workshop's own JavaScript out of an /api/state response that travelled through
    # the portal's proxy. Reaching it means the framed document is the workshop, its script
    # ran, and the proxy hop worked.
    expect(code.locator("#project-name")).to_have_text(portal_harness.PROJECT,
                                                       timeout=30000)
    # An element only the real workshop renders — not a 401 page, not an error body.
    expect(code.locator("#btn-share")).to_have_text("Show someone")
    # And it knows it is embedded, which is why it hides its own duplicate branding and
    # relies on the portal's chrome for the way out.
    expect(code.locator("body")).to_have_class(re.compile(r"\bembedded\b"))
    expect(code.locator(".brand")).to_be_hidden()

    framed = [(u, s) for u, s in seen if u == f"{hosted_workshop.base_url}/workshop/"]
    assert framed and framed[-1][1] == 200, (
        f"the workshop iframe's own navigation did not answer 200: {framed}")

    assert p.page.url.rstrip("/").endswith("/portal"), (
        f"switching to Code navigated the top-level document to {p.page.url!r}")
    _chrome_is_not_covered(p)
    _menu_from_the_code_tab(p, hosted_workshop)

    p.page.screenshot(path=f"{SHOTS}/portal-code-tab.png")
    p.assert_clean("the portal with the workshop showing")


@pytest.mark.parametrize("broken,status,body_says,where,browser_reports", [
    # The workshop is not running: the proxy answers 502. The iframe still navigates
    # there and still gets that raw body -- unavoidable, and enterpriseaiframework-176
    # is precisely that this is no longer what a user is SHOWN, because #code-fallback
    # is an opaque panel over the frame's whole rectangle and app.js now flips it visible
    # for this case. MEASURED, not assumed: this Chromium does log "Failed to load
    # resource … 502" for a subframe navigation that returns an error status, so this
    # case happens to be caught twice over.
    ("down", 502, "Your workshop is not running", "fallback", True),
    # The one that nothing sees. A 200 that is not the workshop — an authenticating proxy
    # in front of the pod serving its own sign-in page, a stale index, another tenant's
    # document. No failed request, no console error, no page error, and the iframe's
    # rectangle in the parent document is exactly where it always is. Its content-type is
    # `text/html`, same as the real workshop, so the fallback panel has no signal to fire
    # on here and must stay hidden -- asserted below as the unchanged path, not left
    # implicit.
    ("impostor", 200, "Sign in to continue", "frame", False),
])
def test_the_route_back_holds_when_the_workshop_does_not(
        browser, hosted_no_workshop, broken, status, body_says, where, browser_reports):
    """Why a box measurement is not the evidence, and what the item asked for under failure.

    Two things are pinned here. First, that `_chrome_is_not_covered` passes in both of
    these — it is a property of the portal's layout and is blind to what the frame
    contains, which is why the content assertions above exist. Second, the thing the item
    is actually about: a user whose Code tab is broken is not stranded. The header, the
    tabs and a working account menu are all still there, and Chat is one click away.

    enterpriseaiframework-176: for `down` specifically, "what the frame contains" is no
    longer the user-visible claim. app.js listens for the frame's `load` (an HTTP error
    status fires `load`, never `error`) and reads the framed document's `contentType`:
    `application/json` means the proxy's own error body, `text/html` means the real page.
    So the assertion that matters for `down` is on #code-fallback, in the parent document
    -- and the frame's raw-JSON body is still checked too, but only as the mechanism the
    panel's visibility depends on, not as what a person sees.
    """
    hosted = hosted_no_workshop
    if broken == "impostor":
        hosted.start_impostor()

    p, seen = _hosted_portal(browser, hosted)
    p.page.click("#tab-code")

    code = p.page.frame_locator("#frame-code")
    if where == "fallback":
        # What a person actually sees: the friendly panel, not FastAPI's JSON.
        expect(p.page.locator("#code-fallback")).to_be_visible(timeout=30000)
        expect(p.page.locator("#code-fallback")).to_contain_text(body_says)
        # What is still true one layer underneath -- the signal the panel's visibility
        # depends on, not the user-visible claim. Kept so that a regression which made
        # the panel appear unconditionally (rather than because the frame really is the
        # error body) would still be caught by something.
        expect(code.locator("body")).to_contain_text(
            "your workshop is not running", timeout=30000)
    else:
        expect(code.locator("body")).to_contain_text(body_says, timeout=30000)
        # The unchanged path, asserted rather than assumed: a 200 impostor must not
        # accidentally trip the panel this item added.
        expect(p.page.locator("#code-fallback")).to_be_hidden()

    # 1. What a box measurement sees: nothing wrong, in either case.
    _chrome_is_not_covered(p)

    # 2. What the browser volunteers.
    reported = p.console_errors + p.failed_requests + p.page_errors
    if browser_reports:
        assert any(str(status) in r for r in reported), (
            f"expected Chromium to report the {status} subframe navigation; it reported "
            f"{reported}. If this stops being true the case is now silent and the content "
            "assertions are the only detector — say so, do not delete the check."
        )
    else:
        assert not reported, (
            "a 200 impostor is supposed to be completely silent — that is the whole point "
            f"of asserting on frame content. Chromium reported: {reported}"
        )

    # 3. What an assertion about the frame's content sees: the workshop is not there.
    framed = [(u, s) for u, s in seen if u == f"{hosted.base_url}/workshop/"]
    assert framed and framed[-1][1] == status, (
        f"expected the framed workshop navigation to answer {status}, got {framed}")
    assert code.locator("#btn-share").count() == 0, (
        "the workshop is not running, so its chrome must not be in the frame")
    assert code.locator("#project-name").count() == 0

    # 4. The claim the item exists for, under the failure: still not stranded.
    _menu_from_the_code_tab(p, hosted)
    p.page.click("#tab-chat")
    expect(p.page.frame_locator("#frame-chat").locator("#chat-stub")).to_be_visible(
        timeout=20000)


# ---------------------------------------------------------------- workshop, self-hosted


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


def _hosted_workshop_page(browser, hosted) -> Page:
    """The workshop on its own, through the shipped proxy, served by the real shell."""
    p, _ = _open(browser, hosted, path="/workshop/")
    p.page.wait_for_selector("#project-name", timeout=30000)
    expect(p.page.locator("#project-name")).to_have_text(portal_harness.PROJECT,
                                                        timeout=30000)
    return p


def test_workshop_renders_with_no_javascript_errors(browser, hosted_workshop):
    p = _hosted_workshop_page(browser, hosted_workshop)
    p.page.screenshot(path=f"{SHOTS}/workshop.png", full_page=True)
    p.assert_clean("workshop")


def test_workshop_terminal_is_the_hero_with_the_drawer_shut(browser, hosted_workshop):
    """The whole point of the rebuild: no preview squatting on half the screen at rest.

    A layout claim about the shipped shell page, so what is in the terminal frame is beside
    the point — this measures the frame's box, and the box is the same size whatever is
    painting inside it. It used to be measured on a real person's pod, which bought it
    nothing and cost them a session.
    """
    p = _hosted_workshop_page(browser, hosted_workshop)
    _close_drawer(p.page)
    term = p.page.locator("#terminal-frame, iframe[src*='terminal']").first
    assert term.count(), "no terminal iframe on the page at all"
    box = term.bounding_box()
    width = p.page.evaluate("() => document.documentElement.clientWidth")
    assert box and box["width"] > width * 0.75, (
        f"terminal is only {box['width'] if box else 0}px of {width} with the drawer shut"
    )
    # The drawer really is shut, so the measurement above is of the state it claims to be
    # of. Without this the test passes on a page whose drawer never opened in the first
    # place for an unrelated reason.
    assert p.page.evaluate(
        "() => document.querySelector('[data-drawer]').dataset.drawer") == "closed"


# ---------------------------------------------------------------- needs a real pod
#
# What is below cannot be hosted. It needs ttyd's own client (xterm.js, WebGL renderer, its
# fit-on-connect behaviour) and a real opencode resolving a real model through the gateway;
# a stand-in for either would turn the test into a test of the stand-in, and the reason
# these exist is that smaller tests were green on a day the terminal came up the wrong size
# and the agent could not spend.
#
# So they are marked, gated and separated instead. `account` cannot reach a person: see
# live_identity.py. conftest.py deselects everything here unless EAI_LIVE_TEST_USER names an
# account whose Secret is marked THROWAWAY.

@pytest.fixture(scope="session")
def base_url() -> str:
    return live_identity.public_base_url()


@pytest.fixture(scope="session")
def account(request) -> tuple[str, str]:
    """The throwaway account, or a loud refusal. Never a person's credential."""
    return live_identity.account(request)


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


@pytest.mark.needs_real_user
def test_the_agent_actually_boots_in_the_terminal(browser, workshop_url, account):
    """End to end: ttyd spawns the shell, the shell starts opencode, opencode finds its
    provider. If the model name is on screen, the whole chain from pod env through the
    config to the gateway catalogue resolved.

    Irreducibly a real pod: the claim is about a real agent process finding a real key and
    a real model catalogue. Nothing on loopback has any of those.
    """
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


@pytest.mark.needs_real_user
def test_the_terminal_keeps_its_size_across_a_reconnect(browser, workshop_url, account):
    """ttyd fits once, from whatever the frame measured when its client started.

    On a reconnect — a project switch, New chat, a reload of that frame — it starts before
    the surrounding layout settles, measures a taller box than it ends up with, and reports
    rows it does not have. Measured before the fix: 50 rows in a pane that fits 38, with an
    unchanged viewport. The agent then drew its input box below the visible area.

    Nothing signals it afterwards, because the WINDOW never resized — only the element did.

    Irreducibly a real pod: `rows`, `cols` and the cell height are ttyd's own xterm.js
    instance reporting what IT fitted to. There is no ttyd off-cluster (no binary on the
    test host) and a stub cannot mis-fit itself, which is the entire subject.
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
