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
import json
import os
import re
import secrets
import subprocess
import time

import httpx
import pytest
from playwright.sync_api import expect, sync_playwright

NS = "enterprise-ai"
REALM = "enterprise-ai"
SHOTS = os.environ.get("BROWSER_SHOT_DIR", "/tmp/eai-shots")
MOBILE_TURN_BUDGET_S = 400


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
def pw():
    """The live Playwright driver object, kept around for `pw.devices[...]` —
    enterpriseaiframework-eb7's mobile tests need real device descriptors (UA, viewport,
    device scale factor, touch), not a hand-rolled viewport dict. See
    `test_mobile_context_is_not_a_resized_desktop_window` for why that distinction is
    load-bearing and tested rather than asserted in a docstring."""
    with sync_playwright() as p:
        yield p


@pytest.fixture(scope="session")
def browser(pw):
    os.makedirs(SHOTS, exist_ok=True)
    b = pw.chromium.launch()
    yield b
    b.close()


@pytest.fixture()
def fresh_browser(pw):
    """A Chromium instance of its own, launched and closed around exactly one test.

    The mobile tests below found `browser` (session-scoped, shared by every test in this
    file) unreliable when a test ran after others had already opened and closed several
    contexts in it: Playwright's own Runtime-mediated calls (`evaluate`, `screenshot`, and
    -- observed directly -- `wait_for_selector` blocking on "waiting for navigation to
    finish") would hang on a plain, un-iframed top-level page, the same SYMPTOM
    enterpriseaiframework-c31 describes for the portal iframe but reproduced here with no
    iframe involved at all. Isolating each mobile test in its own freshly launched browser
    removed it across repeated runs; sharing `browser` did not, even at generous timeouts
    (measured up to 90s, still hung). Filed as enterpriseaiframework note in this test's
    own findings -- this fixture works around it rather than explaining it.
    """
    b = pw.chromium.launch()
    yield b
    b.close()


@pytest.fixture(scope="module")
def fresh_account():
    """A Keycloak account this test creates and destroys itself, for the mobile tests
    that need a REAL sign-in against the live cluster.

    Mirrors `tests-live/test_first_conversation.py::fresh_account` exactly (same admin
    REST flow, same cleanup) rather than importing it: that module is not a fixture
    library, and enterpriseaiframework-eb7's own dispatch scopes this file as the one to
    change. Not `account` (workspace-user-student) above, and deliberately never used for
    anything that opens the Code tab — see enterpriseaiframework-cf5: that fixture is a
    real person's account and the Code tab drives that person's own pod. This identity
    only ever talks to chat directly (`{base_url}/`, never the portal iframe, never
    `/workshop/`), which touches no pod at all.
    """
    pf = subprocess.Popen(
        ["kubectl", "-n", NS, "port-forward", "svc/identity", "0:8080"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    user_id = None
    idp = None
    headers = None
    try:
        line = pf.stdout.readline()
        m = re.search(r":(\d+)\s*->", line)
        if not m:
            pytest.fail(f"could not read the forwarded port from kubectl's output: {line!r}")
        idp = f"http://localhost:{m.group(1)}"

        for _ in range(40):
            try:
                if httpx.get(f"{idp}/realms/{REALM}", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(1)
        else:
            pytest.fail("identity provider never answered on the port-forward")

        admin_user = _secret("enterprise-ai-secrets", "IDP_ADMIN_USER")
        admin_password = _secret("enterprise-ai-secrets", "IDP_ADMIN_PASSWORD")
        token_resp = httpx.post(
            f"{idp}/realms/master/protocol/openid-connect/token",
            data={"grant_type": "password", "client_id": "admin-cli",
                  "username": admin_user, "password": admin_password},
            timeout=30,
        )
        token_resp.raise_for_status()
        token = token_resp.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

        username = f"mobile-{secrets.token_hex(3)}"
        password = secrets.token_urlsafe(24)

        created = httpx.post(
            f"{idp}/admin/realms/{REALM}/users", headers=headers,
            json={"username": username, "email": f"{username}@example.invalid",
                  "firstName": "Mobile", "lastName": "Tester",
                  "enabled": True, "emailVerified": True, "requiredActions": []},
            timeout=30,
        )
        assert created.status_code == 201, (
            f"could not create {username}: {created.status_code} {created.text[:300]}"
        )
        user_id = created.headers["Location"].rsplit("/", 1)[-1]

        httpx.put(
            f"{idp}/admin/realms/{REALM}/users/{user_id}/reset-password", headers=headers,
            json={"type": "password", "value": password, "temporary": False}, timeout=30,
        ).raise_for_status()

        yield username, password
    finally:
        if user_id and idp and headers:
            httpx.delete(f"{idp}/admin/realms/{REALM}/users/{user_id}",
                        headers=headers, timeout=30)
        pf.terminate()
        try:
            pf.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pf.kill()


@pytest.fixture()
def fresh_workspace(fresh_account):
    """Provisions `fresh_account`'s OWN workspace pod on the live cluster --
    `deploy/bin/provision-workspace.sh`, the exact script an operator runs to stand up a
    real user's Code tab, not a substitute for it.

    enterpriseaiframework-eb7 Challenge 3: control-plane commit de2f021 already ruled that
    nothing on loopback can stand in for "a real ttyd, a real xterm.js and a real opencode
    resolving a real model through the gateway" -- portal_harness's ttyd stub proves only
    that a keystroke's ROUTE survives two nested iframes and the real proxy. This is the
    route that gets the real thing without touching a real person's own pod: verified
    directly in this session (kubectl apply, a real rollout, and a real opencode painting
    "Ask anything" / "GLM 5.2 Enterprise AI" into the terminal buffer at an iPhone 13
    device profile, torn down cleanly afterward). If provisioning ever fails here, this
    fixture fails loudly with the command and its exit code rather than falling back to a
    stub -- that failure IS the proof of inability the item asks for, not a reason to
    quietly re-stub.

    WORKSPACE_TAG prefers an image already pushed for the most recent commit that touched
    `deploy/workspace/` (the normal `deploy/bin/kaniko-build.sh deploy/workspace
    <registry>/enterprise-ai-workspace:$(git rev-parse --short HEAD)` an operator runs per
    deploy/README.md, checked against the registry's own tag list, not assumed) -- NOT the
    repo's overall HEAD, which changes on every commit including ones (like this file)
    that never touch the image at all and would otherwise make this fixture flap onto a
    stale, unbuilt tag every time an unrelated commit landed. Falls back to `ws-student`'s
    own already-running image if no build exists yet for that path's latest commit.
    """
    username, _ = fresh_account
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    image_commit = subprocess.run(
        ["git", "-C", root, "log", "-1", "--format=%h", "--", "deploy/workspace"],
        capture_output=True, text=True, timeout=15,
    ).stdout.strip()
    tag = None
    if image_commit:
        try:
            listing = httpx.get(
                "http://192.168.2.43:30500/v2/enterprise-ai-workspace/tags/list", timeout=10
            ).json()
            if image_commit in (listing.get("tags") or []):
                tag = image_commit
        except Exception:
            pass  # registry unreachable or unexpected shape -- fall back below
    if not tag:
        tag = subprocess.run(
            ["kubectl", "-n", NS, "get", "deploy", "ws-student",
             "-o", "jsonpath={.spec.template.spec.containers[0].image}"],
            capture_output=True, text=True, timeout=30,
        ).stdout.rsplit(":", 1)[-1]
    if not tag:
        pytest.fail(
            "no image tagged with this worktree's HEAD in the rail registry, AND could "
            f"not read ws-student's image tag from the live cluster (kubectl -n {NS} get "
            "deploy ws-student ...) -- no known-good WORKSPACE_TAG to provision with, and "
            "guessing one is worse than failing loudly"
        )
    # provision-workspace.sh forwards Keycloak admin and control-plane traffic over
    # hardcoded local ports (18080/18081) and does not fail loudly if that tunnel loses
    # the race at startup -- MEASURED directly in this session: one run in five got a
    # "curl: (7) Failed to connect to localhost port 18080" and a KeyError from the admin
    # user lookup parsing an auth-failure body as a list; the immediate next run (same
    # command, same machine) succeeded cleanly. The script calls itself "Repeatable and
    # idempotent: run it twice and you get the same workspace" -- this is that guarantee
    # used deliberately, not a timeout widened to paper over a real failure.
    proc = None
    for attempt in range(2):
        proc = subprocess.run(
            ["bash", os.path.join(root, "deploy", "bin", "provision-workspace.sh"), username],
            cwd=root, env={**os.environ, "WORKSPACE_TAG": tag},
            capture_output=True, text=True, timeout=300,
        )
        if proc.returncode == 0:
            break
    if proc.returncode != 0:
        pytest.fail(
            f"provisioning a workspace for the throwaway account {username!r} failed "
            f"twice in a row (deploy/bin/provision-workspace.sh, exit {proc.returncode}) "
            f"-- this is a proven inability, not grounds to fall back to a stub:\n"
            f"stdout: {proc.stdout[-2000:]}\nstderr: {proc.stderr[-2000:]}"
        )
    try:
        yield username
    finally:
        for args in (
            ["delete", "deployment,svc,pvc", "-l", f"workspace.enterprise-ai/user={username}",
             "--wait=false"],
            ["delete", "secret", f"ws-{username}-key", "--ignore-not-found"],
            ["delete", "configmap", f"workspace-memory-{username}", "--ignore-not-found"],
        ):
            subprocess.run(["kubectl", "-n", NS, *args],
                            capture_output=True, text=True, timeout=30)


# ------------------------------------------------------------------ raw CDP, mobile only
#
# enterpriseaiframework-c31 (wave 6): submitting a login form in a MOBILE Playwright
# context, when the destination does a client-side (pushState) route change afterward --
# as LibreChat's chat surface does, landing on /c/<conversationId> -- leaves Playwright's
# OWN navigation-lifecycle tracking confused: `page.url` stays pinned at the Keycloak auth
# URL forever, and every navigation-gated Playwright API (`wait_for_selector`,
# `page.evaluate`, `page.screenshot`) then waits on a navigation that, as far as Playwright
# is concerned, never finished. MEASURED: test_mobile_sign_in_and_a_real_conversation
# failed 3/3 at the #prompt-textarea wait before this existed, while a scratch probe
# reading the SAME page over raw CDP at the same moment found the real document already at
# /c/new with #prompt-textarea present and zero console errors -- the product is fine;
# only Playwright's own binding lost the thread. The fix, proven by that probe and now the
# house pattern for this file: reach the same facts through
# `browser_context.new_cdp_session(page)` + `Runtime.evaluate` instead of Playwright's
# locator/evaluate/screenshot APIs, which never routes through whatever broke.
#
# NOT needed for the workshop/Code surface (see fresh_workspace's own test below): that
# route is a plain server-rendered redirect chain with no client-side route push, and this
# item's own scratch probe measured ordinary Playwright waits working correctly against it
# in a mobile context, end to end, including reading the real terminal's buffer.


def _cdp_eval(session, expression: str):
    result = session.send("Runtime.evaluate", {
        "expression": expression, "returnByValue": True, "awaitPromise": True,
    })
    exc = result.get("exceptionDetails")
    if exc:
        raise RuntimeError(f"CDP Runtime.evaluate raised: {json.dumps(exc)[:500]}")
    return result["result"].get("value")


def _cdp_wait_for(session, expression: str, timeout_s: float, what: str, poll_s: float = 1.0):
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        last = _cdp_eval(session, expression)
        if last:
            return last
        time.sleep(poll_s)
    raise AssertionError(
        f"timed out after {timeout_s}s waiting for {what} over raw CDP (not a Playwright "
        f"navigation-gated wait -- see this file's CDP-helper comment); last observed "
        f"value: {last!r}"
    )


def _cdp_wait_for_selector(session, selector: str, timeout_s: float, what: str | None = None):
    return _cdp_wait_for(session, f"!!document.querySelector({selector!r})", timeout_s,
                          what or f"selector {selector!r} to appear")


def _cdp_type(session, selector: str, text: str):
    """Focus SELECTOR and insert TEXT through CDP's own input pipeline
    (`Input.insertText`) -- the same browser-level mechanism a real keyboard uses, proven
    by enterpriseaiframework-c31's own probe to register with LibreChat's React-controlled
    composer (typed text appeared in the composer and the send button became enabled)."""
    _cdp_eval(session, f"document.querySelector({selector!r}).focus()")
    session.send("Input.insertText", {"text": text})


def _cdp_click(session, selector: str):
    _cdp_eval(session, f"document.querySelector({selector!r}).click()")


def _cdp_screenshot(session, path: str):
    result = session.send("Page.captureScreenshot", {"format": "png"})
    with open(path, "wb") as f:
        f.write(base64.b64decode(result["data"]))


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


# ------------------------------------------- the route back to the portal, self-hosted
#
# These two do not use the cluster. The Code tab drives its user's own pod — switching to
# it starts a session and writes that user's session bookkeeping — so a test that proved
# this against the live deployment would be mutating a real person's state to do it. The
# stack is hosted on loopback instead; see tests-live/portal_harness.py for exactly what
# in it is the shipped code and what is a marker page.


@pytest.fixture(scope="module")
def hosted():
    """The portal, its workshop proxy and a throwaway workshop, on loopback."""
    import portal_harness

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


def _hosted_portal(browser, hosted, context_kwargs=None) -> tuple[Page, list[tuple[str, int]]]:
    """The portal as the harness serves it, plus every response status the page saw.

    The statuses are collected because `requestfailed` does not fire on an HTTP error
    response — it is for transport failures — so a framed surface answering 401 or 500 is
    not a "failed request" as far as Playwright is concerned. Recording the response is how
    the test sees the status the frame actually got.

    `context_kwargs`: defaults to the desktop viewport every existing caller here relies
    on. The mobile tests pass `pw.devices["iPhone 13"]` instead — a real device
    descriptor (UA, viewport, device scale factor, `has_touch`), not a resized desktop
    window; see `test_mobile_context_is_not_a_resized_desktop_window` for why that
    distinction is enforced rather than assumed.
    """
    ctx = browser.new_context(**(context_kwargs or {"viewport": {"width": 1440, "height": 900}}))
    _CONTEXTS.append(ctx)
    p = Page(ctx.new_page())
    seen: list[tuple[str, int]] = []
    p.page.on("response", lambda r: seen.append((r.url, r.status)))
    p.page.goto(f"{hosted.base_url}/portal/", wait_until="domcontentloaded", timeout=45000)
    p.page.wait_for_selector("#tab-chat", timeout=20000)
    return p, seen


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
    import portal_harness

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
    import portal_harness

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


# ---------------------------------------------------------------- mobile
#
# enterpriseaiframework-eb7: the whole product usable on a phone -- chat, Code, portal,
# published work -- ALL FOUR, on a real viewport, not a desktop window resized. Every test
# below uses `pw.devices["iPhone 13"]` (real UA, real viewport, real device scale factor,
# `has_touch`) and taps rather than clicks for at least its primary interaction: `.tap()`
# is a touch-only Playwright API that raises outright on a context that was not created
# with `has_touch=True`, so a test written this way cannot silently pass unchanged in a
# desktop context -- it would blow up before making a single assertion. The control below
# proves that refusal is real rather than assuming it, because it is exactly the check
# named in this item's own dispatch as what an adversary reaches for first.


def test_mobile_context_is_not_a_resized_desktop_window(fresh_browser, pw, hosted):
    """THE control an adversary reaches for first on a mobile item: run the same
    assertions in a desktop context and see whether they still pass.

    Three independent signals, all real and none inferred from a viewport number alone:
      1. `navigator.userAgent` -- what the PAGE itself sees, which is what a real phone's
         site would see and a merely-resized desktop window would not.
      2. `.tap()` -- Playwright's own touch-only API, which raises on a context that was
         not created with `has_touch=True`. This is not a property of the page; it is
         Playwright refusing to fake a touch it cannot produce.
      3. `.brandname`'s CSS visibility -- a genuine, pre-existing responsive rule
         (`@media (max-width: 760px) { .brandname { display: none } }`,
         control-plane/app/portal_static/style.css) that a 390px-wide mobile viewport
         crosses and a 1440px desktop one does not.
    """
    mobile = fresh_browser.new_context(**pw.devices["iPhone 13"])
    _CONTEXTS.append(mobile)
    desktop = fresh_browser.new_context(viewport={"width": 1440, "height": 900})
    _CONTEXTS.append(desktop)

    mobile_page = mobile.new_page()
    mobile_page.goto(f"{hosted.base_url}/portal/", wait_until="domcontentloaded", timeout=45000)
    mobile_page.wait_for_selector("#tab-chat", timeout=20000)

    desktop_page = desktop.new_page()
    desktop_page.goto(f"{hosted.base_url}/portal/", wait_until="domcontentloaded", timeout=45000)
    desktop_page.wait_for_selector("#tab-chat", timeout=20000)

    # 1. What the page itself observes.
    assert mobile_page.evaluate("() => /Mobi|iPhone/.test(navigator.userAgent)"), (
        "the mobile context's own UA string does not read as mobile"
    )
    assert not desktop_page.evaluate("() => /Mobi|iPhone/.test(navigator.userAgent)"), (
        "a plain desktop context should not carry a mobile UA -- if it does, this control "
        "proves nothing"
    )

    # 2. Playwright's own touch gate: succeeds on mobile, raises on desktop.
    mobile_page.locator("#tab-code").tap()
    with pytest.raises(Exception, match="hasTouch"):
        desktop_page.locator("#tab-code").tap(timeout=2000)

    # 3. A genuine CSS divergence the two widths actually produce.
    assert mobile_page.locator(".brandname").is_hidden(), (
        "the mobile viewport (390px) should trip the <=760px rule that hides .brandname"
    )
    assert desktop_page.locator(".brandname").is_visible(), (
        "the desktop viewport (1440px) should NOT trip that rule -- if it does not show "
        "the brand name either, this control is not discriminating on width at all"
    )


def test_mobile_sign_in_and_a_real_conversation(fresh_browser, pw, base_url, fresh_account):
    """SURFACES 1 and 2 of enterpriseaiframework-eb7's four: sign-in and holding a
    conversation, on the live cluster, on a real mobile device profile.

    Driven directly at `{base_url}/`, not through the portal shell's iframe -- the same
    entry point test_chat_login.py and test_first_conversation.py already prove works:
    OPENID_AUTO_REDIRECT bounces there to Keycloak with no button to tap.

    Uses `fresh_account`, not `account` (workspace-user-student): mints and deletes its
    own throwaway Keycloak identity so no real person's session is touched
    (enterpriseaiframework-cf5's hazard). It never opens the Code tab or `/workshop/`, so
    it drives no workspace pod at all -- the identity only has to exist in Keycloak for
    chat's OIDC login and LibreChat's own auto-provisioned account row.

    enterpriseaiframework-eb7 Challenge 1: everything from the login submit onward is read
    over raw CDP, not Playwright's own navigation-gated APIs -- see this file's CDP-helper
    comment above for why (measured: this test failed 3/3 at a plain
    `wait_for_selector("#prompt-textarea")` while the real document had already landed
    cleanly, zero console errors, confirmed by a scratch probe reading the same page over
    CDP at the same moment). The sign-in step itself (finding the login form, filling it,
    tapping submit) is unaffected and stays ordinary Playwright -- the hang starts only
    after LibreChat's post-login client-side route push.
    """
    username, password = fresh_account
    ctx = fresh_browser.new_context(**pw.devices["iPhone 13"], ignore_https_errors=True)
    _CONTEXTS.append(ctx)
    p = Page(ctx.new_page())
    cdp = ctx.new_cdp_session(p.page)

    p.page.goto(f"{base_url}/", wait_until="load", timeout=60000)

    # SURFACE 1: sign-in. A real Keycloak login form, rendered at a real mobile
    # viewport/UA, reached and SUBMITTED with a touch tap -- not merely present.
    p.page.wait_for_selector("input[name='username']", timeout=30000)
    p.page.fill("input[name='username']", username)
    p.page.fill("input[name='password']", password)
    p.page.locator("input[type='submit'], button[type='submit']").first.tap(
        timeout=10000, no_wait_after=True)

    # SURFACE 2: the composer accepts input and a reply comes back. Polled over raw CDP
    # from here on (see this file's CDP-helper comment).
    _cdp_wait_for_selector(cdp, "#prompt-textarea", 60,
                            "the chat composer to appear after mobile sign-in")
    body_text = _cdp_eval(cdp, "document.body.innerText")
    assert "Sign in with" not in body_text, (
        "chat asked a brand-new mobile sign-in for a second login"
    )
    overflow = _cdp_eval(
        cdp, "document.documentElement.scrollWidth - document.documentElement.clientWidth")
    assert overflow <= 0, f"chat scrolls sideways by {overflow}px on a real iPhone 13 viewport"
    _cdp_screenshot(cdp, f"{SHOTS}/mobile-chat-landed.png")

    # Typed through CDP's own input pipeline (Input.insertText), not page.fill -- the same
    # mechanism the c31 probe proved React's controlled composer actually picks up.
    _cdp_type(cdp, "#prompt-textarea", "Say the single word hello and nothing else.")
    _cdp_click(cdp, '[data-testid="send-button"]')
    _cdp_screenshot(cdp, f"{SHOTS}/mobile-chat-sent.png")

    _cdp_wait_for_selector(cdp, '[data-testid="copy-response-button"]',
                            MOBILE_TURN_BUDGET_S, "a finished assistant reply")
    reply = _cdp_eval(cdp, "document.body.innerText")
    _cdp_screenshot(cdp, f"{SHOTS}/mobile-chat-reply.png")

    lower = reply.lower()
    for bad in ("something went wrong", "an error occurred"):
        assert bad not in lower, f"the mobile turn surfaced an error signature: {bad!r}"

    assert not p.console_errors, f"console errors during the mobile turn: {p.console_errors}"


def test_mobile_code_tab_renders_and_accepts_a_keystroke_with_chrome_intact(
        fresh_browser, pw, hosted_workshop):
    """SURFACE 3: Code, the chrome and iframe-routing half. The item calls this the case
    'most likely to be broken and hardest to see' -- the workshop is an iframe inside the
    portal, itself nesting a terminal iframe. This drives the same hosted, loopback stack
    `test_the_portal_is_reachable_from_inside_the_workshop_tab` already proves the desktop
    case against: portal.require_user, portal.me, workshop.workshop_proxy and a real
    shell-server subprocess, all real and shipped, with a real mobile device profile
    instead of a desktop one.

    'Renders' is the same content assertions as the desktop test (project name, share
    button, embedded class). 'Accepts a keystroke' is a route proof, not an agent proof: a
    tap-to-focus plus a real typed string, checked against the stub's own echo of its
    `input` event -- see portal_harness._make_ttyd_stub for exactly what that does and
    does not prove (it proves the keystroke's ROUTE through two nested iframes and the
    real proxy; it does NOT prove a real agent responds -- that is
    test_mobile_code_tab_boots_the_real_agent_in_a_provisioned_pod below, against a real
    pod, per enterpriseaiframework-eb7 Challenge 3 and cf5's ruling that nothing on
    loopback can stand in for that).
    """
    import portal_harness

    p, seen = _hosted_portal(fresh_browser, hosted_workshop, context_kwargs=pw.devices["iPhone 13"])

    expect(p.page.frame_locator("#frame-chat").locator("#chat-stub")).to_be_visible(
        timeout=20000)

    p.page.locator("#tab-code").tap()
    code = p.page.frame_locator("#frame-code")
    expect(code.locator("#project-name")).to_have_text(portal_harness.PROJECT, timeout=30000)
    expect(code.locator("#btn-share")).to_have_text("Show someone")
    expect(code.locator("body")).to_have_class(re.compile(r"\bembedded\b"))

    # A real, shipped first-run overlay (deploy/workspace/shell/app.js, K_CURTAIN) covers
    # the whole shell until dismissed -- a genuinely fresh browser context (no prior
    # localStorage) sees it every time, on a phone exactly as on a desktop. Dismissed the
    # way a person would: tapping its own "OK, I'm ready" button.
    curtain_go = code.locator("#curtain-go")
    if curtain_go.is_visible():
        curtain_go.tap()

    # The nested terminal iframe -- one level deeper than #frame-code, reached the same
    # way a real ttyd would be: workshop_proxy routes "terminal/*" sub-paths to the ttyd
    # port rather than the shell port.
    terminal = code.frame_locator("#terminal-frame")
    stub_input = terminal.locator("#ttyd-input")
    stub_input.tap()
    stub_input.press_sequentially("hello from a phone")
    expect(terminal.locator("#ttyd-echo")).to_have_text("hello from a phone", timeout=10000)

    # The claim the item names explicitly: the header stays above the frame, on a
    # viewport with far less vertical room than the desktop version of this test has.
    _chrome_is_not_covered(p)

    overflow = p.page.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
    assert overflow <= 0, f"the portal scrolls sideways by {overflow}px with Code showing"

    p.page.screenshot(path=f"{SHOTS}/mobile-code-tab.png")
    p.assert_clean("the mobile portal with the workshop showing")

    # enterpriseaiframework-eb7 Challenge 2's negative control (wave 6 adversary):
    # overriding devices['iPhone 13'] to a full desktop viewport+UA with only has_touch
    # left every assertion above passing unchanged -- has_touch is orthogonal to viewport
    # and UA, so .tap() alone blocks nothing. This is the control SHIPPED AS a test, not
    # assumed: a genuine, pre-existing responsive rule
    # (`@media (max-width: 760px) { .brandname { display: none } }`,
    # control-plane/app/portal_static/style.css) that this 390px context crosses and a
    # 1440px one does not -- measured on the SAME hosted stack, with the Code tab showing.
    desktop_p, _ = _hosted_portal(fresh_browser, hosted_workshop,
                                   context_kwargs={"viewport": {"width": 1440, "height": 900}})
    desktop_p.page.locator("#tab-code").click()
    expect(desktop_p.page.frame_locator("#frame-code").locator("#project-name")).to_have_text(
        portal_harness.PROJECT, timeout=30000)
    assert p.page.locator(".brandname").is_hidden(), (
        "the mobile viewport (390px) should trip the <=760px rule that hides .brandname"
    )
    assert desktop_p.page.locator(".brandname").is_visible(), (
        "the desktop viewport (1440px) should NOT trip that rule -- if it does not show "
        "the brand name either, this control is not discriminating on width at all"
    )


def test_mobile_code_tab_boots_the_real_agent_in_a_provisioned_pod(
        fresh_browser, pw, workshop_url, fresh_account, fresh_workspace):
    """SURFACE 3, the real-agent half: enterpriseaiframework-eb7 Challenge 3.

    control-plane commit de2f021 already ruled that nothing on loopback can stand in for
    "a real ttyd, a real xterm.js and a real opencode resolving a real model through the
    gateway" -- portal_harness._make_ttyd_stub proves only that a keystroke's ROUTE
    survives two nested iframes and the real proxy, which is exactly why the test above
    says so rather than claiming more. This test drives the REAL terminal instead, in
    `fresh_workspace`'s own freshly provisioned pod (belonging to `fresh_account`'s
    throwaway identity, never a real person's -- enterpriseaiframework-cf5's hazard), at a
    real iPhone 13 device profile -- mirroring the desktop proof
    (`test_the_agent_actually_boots_in_the_terminal`) exactly, model included.

    Driven at `workshop_url` directly, not through the portal's `#frame-code` -- this is
    the SAME shell page either way (workshop.workshop_proxy serves it at both), and going
    straight there is what the existing desktop live-cluster test already does. Not
    wrapped in raw CDP: unlike the chat surface (Challenge 1), the workshop route is a
    plain server-rendered redirect chain with no client-side route push, and this item's
    own scratch probe measured ordinary Playwright waits working correctly against it in a
    mobile context -- verified directly: goto, form fill, tap-submit and a polled
    `_terminal_buffer` read all completed normally, painting "Ask anything" and "GLM 5.2
    Enterprise AI" into a real xterm.js buffer inside two nested iframes.
    """
    username, password = fresh_account
    assert fresh_workspace == username

    ctx = fresh_browser.new_context(**pw.devices["iPhone 13"], ignore_https_errors=True)
    _CONTEXTS.append(ctx)
    p = Page(ctx.new_page())

    p.page.goto(f"{workshop_url}/", wait_until="load", timeout=60000)
    if p.page.locator("input[name='username']").count():
        p.page.fill("input[name='username']", username)
        p.page.fill("input[name='password']", password)
        p.page.locator("input[type='submit'], button[type='submit']").first.tap(
            timeout=10000, no_wait_after=True)
        p.page.wait_for_load_state("load", timeout=60000)

    buf = ""
    for _ in range(24):          # opencode paints in ~5s; allow for a cold pod
        p.page.wait_for_timeout(2500)
        buf = _terminal_buffer(p.page)
        if "Ask anything" in buf or "GLM" in buf:
            break
    assert buf.strip(), "the terminal never painted anything at all on a real iPhone 13 viewport"
    assert ("Ask anything" in buf) or ("ctrl+p commands" in buf), (
        f"opencode never reached a usable prompt on mobile. Buffer:\n{buf[:600]}")
    assert "GLM" in buf, (
        "the agent started on mobile but shows no model -- its provider config did not "
        f"resolve. Buffer:\n{buf[:600]}"
    )

    overflow = p.page.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
    assert overflow <= 0, f"the workshop scrolls sideways by {overflow}px at iPhone 13 width"

    p.page.screenshot(path=f"{SHOTS}/mobile-real-terminal.png")
    p.assert_clean("the real terminal, in a real provisioned pod, on a real iPhone 13 viewport")


def test_mobile_reads_published_work(fresh_browser, pw, hosted, base_url, account):
    """SURFACE 4: published work. Opens the account menu with a tap and follows 'Your
    shared work' exactly the way a person would on a phone -- the menu, the link's target
    and the page it lands on are all real, shipped code (portal.me's links payload, the
    shipped index.html/app.js); only what is AT that URL, in this first leg, is the
    harness's marker stub (see portal_harness.py's own module docstring for why: what is
    under test in THIS leg is whether the portal's link reaches the right URL, not the
    static-file server behind it -- the second leg below reaches the real one).
    """
    import portal_harness

    p, seen = _hosted_portal(fresh_browser, hosted, context_kwargs=pw.devices["iPhone 13"])

    p.page.locator("#avatar-btn").tap()
    p.page.wait_for_selector("#user-menu:not([hidden])", timeout=8000)
    assert p.page.inner_text("#username").strip() == portal_harness.USER

    with p.page.context.expect_page() as popup:
        p.page.locator("#mi-published").tap()
    opened = popup.value
    opened.wait_for_load_state("load", timeout=20000)
    assert opened.url == hosted.published_url, (
        f"'Your shared work' reached {opened.url!r} on mobile, expected "
        f"{hosted.published_url!r}"
    )
    # Read as rendered DOM, not merely "the request succeeded" -- the marker text is what
    # a person actually sees.
    expect(opened.locator("#published-stub")).to_be_visible(timeout=10000)
    assert portal_harness.USER in opened.locator("#published-stub").inner_text()
    opened.close()

    # enterpriseaiframework-eb7 Challenge 2's negative control (wave 6 adversary): the
    # assertions above are IDENTICAL to what test_the_portal_is_reachable_from_inside_the_
    # workshop_tab's own _menu_from_the_code_tab already proves on desktop, so by
    # themselves they add no mobile evidence, and the adversary's negative control
    # (devices['iPhone 13'] overridden to a desktop viewport+UA with only has_touch
    # enabled) passed them unchanged. This does not: a genuine, pre-existing CSS
    # breakpoint the 390px context crosses and a 1440px one does not
    # (control-plane/app/portal_static/style.css).
    desktop_p, _ = _hosted_portal(fresh_browser, hosted,
                                   context_kwargs={"viewport": {"width": 1440, "height": 900}})
    assert p.page.locator(".brandname").is_hidden(), (
        "the mobile viewport (390px) should trip the <=760px rule that hides .brandname"
    )
    assert desktop_p.page.locator(".brandname").is_visible(), (
        "the desktop viewport (1440px) should NOT trip that rule -- if it does not show "
        "the brand name either, this control is not discriminating on width at all"
    )

    p.assert_clean("the mobile portal after reading published work")

    # enterpriseaiframework-eb7 Challenge 4: what is AT the URL in the leg above is the
    # harness's own marker stub, by design -- it proves the LINK, not what a phone
    # actually renders once it lands. The real thing is live and reachable with no
    # account, no pod and no disk (curl -sk https://gateway.tailcb6ef9.ts.net:8443/
    # live/<user>/ returns 200 with a real listing); this leg hits it directly, on the
    # LIVE cluster (base_url, not the harness), with a real iPhone 13 device profile, and
    # measures how it actually renders at 390px rather than asserting a stub.
    #
    # Uses `account` (workspace-user-student), not a fresh mint: reading a public,
    # unauthenticated static listing touches no pod and mutates nothing, so
    # enterpriseaiframework-cf5's hazard (which is specifically about the Code tab driving
    # a real person's pod) does not apply -- this is the same fixture the desktop tests in
    # this file already use for non-Code-tab portal reads.
    username = account[0]
    live_url = f"{base_url}/live/{username}/"
    precheck = httpx.get(live_url, verify=False, timeout=15)
    assert precheck.status_code == 200, (
        f"the live published listing for {username!r} answered {precheck.status_code}, "
        f"not 200, at {live_url!r} -- this leg needs something already published for this "
        "account (see test_e2e_journey.py / the Code tab's publish flow for how "
        "workspace-user-student acquires its published project)"
    )

    live_ctx = fresh_browser.new_context(**pw.devices["iPhone 13"], ignore_https_errors=True)
    _CONTEXTS.append(live_ctx)
    live_page = live_ctx.new_page()
    live_page.goto(live_url, wait_until="load", timeout=30000)
    assert live_url.rstrip("/") in live_page.url.rstrip("/"), (
        f"landed on {live_page.url!r} instead of the real published listing"
    )
    body = live_page.evaluate("() => document.body.innerText")
    assert body.strip(), "the real published listing rendered no content at all on a phone"
    overflow = live_page.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
    assert overflow <= 0, (
        f"the real published site scrolls sideways by {overflow}px on a real iPhone 13 "
        "viewport (390px) -- measured against the LIVE site, not a stub"
    )
    live_page.screenshot(path=f"{SHOTS}/mobile-published-live.png", full_page=True)
    opened.close()

    p.assert_clean("the mobile portal after reading published work")
