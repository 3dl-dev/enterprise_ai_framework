"""enterpriseaiframework-03f: a brand-new account reaches a working first conversation
with no verbal instruction and nothing hand-configured.

WHY THIS EXISTS SEPARATELY FROM test_portal.py AND test_e2e_journey.py

Both of those sign in with a fixture account (`workspace-user-student`) that has already
signed in before, in prior test runs -- a RETURNING account. Neither one ever sends a real
message through the chat composer and reads back a real reply; test_portal.py only walks
the portal's own JSON API, and test_e2e_journey.py's conversation happens in the Code tab's
terminal, not in Chat.

This item's own DONE condition is specifically about an account that has NEVER signed in
reaching a WORKING FIRST CONVERSATION -- "measured by driving it, not by reading the UI."
Nothing in the suite proved that combination before this file: a genuinely fresh identity,
the documented front door and nothing else, no tab clicked by instruction, no model chosen,
no tool named, and a real assistant reply read back out of the chat DOM.

WHAT "NOTHING HAND-CONFIGURED" MEANS HERE, PRECISELY

The fixture below creates a Keycloak user directly against the identity provider's admin
API -- the one, unavoidable step an operator takes to add somebody to the realm (the same
precondition scope item 1 already assumes). It deliberately calls neither
`POST /admin/sync` nor `POST /admin/keys/issue` on the control plane. If chat needed either
of those to work, this test would fail, because the chat surface authenticates through one
shared virtual key (`chat-surface::chat`, control-plane/app/chat_identity.py) and LibreChat
auto-provisions its own account record on first OIDC login -- nothing per-user has to be
minted before a first message can go out.

WHAT THIS DOES NOT TOUCH, ON PURPOSE

Two adjacent defects are already filed and explicitly out of scope here
(enterpriseaiframework-c8b: code execution advertised against a codeapi backend that is not
deployed; enterpriseaiframework-ce2: the published-work link 404s for anyone who has not
published). This test picks a prompt that does not call for code execution (a factual
question, not a computation) and never opens the account menu's "Your shared work" link, so
neither defect is exercised as a side effect of proving this item.

enterpriseaiframework-222 (THIS ITEM'S FIX). The prior wave's version of this file asserted
only that a `[data-testid="copy-response-button"]` appeared in the DOM -- true the instant
ANY assistant message finishes, including a message whose entire content is the tool call
LEAKED AS PLAIN TEXT (measured on the live cluster 2026-07-31: a persisted message reading
'I will check the current Python version for you.:::tool\n{"name": "web_search", ...}\n'
with `unfinished:false, error:false`, which renders a perfectly normal-looking finished
turn with a copy button and answers nothing). That assertion could only ever time out or
pass; it could not tell a real grounded answer apart from that failure. The root cause was
`bundle/librechat/librechat.yaml`'s modelSpecs never setting `webSearch: true` -- so a
genuinely new conversation's `ephemeralAgent.web_search` resolved to `false`
(`client/src/utils/endpoints.ts#applyModelSpecEphemeralAgent`, `modelSpec.webSearch ??
false`, with no localStorage override to layer on top because there is no prior
conversation for a brand-new account) even though the deployed system prompt
unconditionally instructs the model to search the web. Told to use a tool it was never
given, the model fabricated one, imitating the ":::artifact" fence convention taught two
paragraphs below in the same prompt.

This file now reads the actual PERSISTED message back over the surface's own API
(`GET /api/messages/<conversationId>`, the same mechanism `tests/chat_turn.py` uses) rather
than trusting only the DOM, and asserts on its `content` blocks: a real `tool_call` block
naming `web_search` must be present (LibreChat only ever creates that block by actually
invoking the tool -- there is no code path that fabricates one from prose), and the
negative control asserts the visible reply text does NOT contain the literal leaked-prose
shape (`:::tool`, a `"name": "web_search"` JSON fragment sitting in running text). An
assertion set that passed with the tool call emitted as text would have proven nothing --
the DOM alone could not tell the two apart, which is exactly why the prior wave's version
did not catch this.

Real cluster, real model, a fraction of a cent. Kept out of `make test` for the same reason
test-e2e and test-browser are: it needs a live cluster and waits on a real model.
"""

import base64
import json
import re
import subprocess
import secrets
import time

import httpx
import pytest
from playwright.sync_api import sync_playwright

NS = "enterprise-ai"
SHOTS = "/tmp/eai-shots"
REALM = "enterprise-ai"
# A plain factual question, no tool named. Matches the prompt shape
# tests-live/test_tool_selection.py measured selecting web_search at 4/4 on the baseline
# system prompt -- chosen so this test exercises the same "reaches for a tool unprompted"
# claim enterpriseaiframework-e6f built, without ever touching code execution.
PROMPT = "What is the latest stable version of Python?"
TURN_BUDGET_S = 400

# The exact leaked-prose shape measured on the live cluster 2026-07-31 (librechat message
# _id 6a6c37c5838bf62c798bc784). If any of these substrings shows up in the VISIBLE reply
# text, the model wrote a tool call out as prose instead of invoking it -- the defect this
# item fixes -- regardless of whether some other assertion in this file happens to pass.
LEAKED_TOOL_CALL_SIGNATURES = (
    ":::tool",
    '"name": "web_search"',
    "'name': 'web_search'",
)


def _secret(name: str, key: str) -> str:
    out = subprocess.run(
        ["kubectl", "-n", NS, "get", "secret", name, "-o", f"jsonpath={{.data.{key}}}"],
        capture_output=True, text=True, timeout=60, check=True,
    ).stdout
    if not out:
        pytest.fail(f"secret {name}/{key} is empty or missing")
    return base64.b64decode(out).decode()


@pytest.fixture(scope="module")
def base_url() -> str:
    return _secret("enterprise-ai-secrets", "PUBLIC_BASE_URL").rstrip("/")


@pytest.fixture(scope="module")
def fresh_account():
    """A Keycloak account this test creates and destroys itself.

    Not `ensure-second-user.sh`'s persistent fixture users (student, claire, ...): those
    are reused across runs by design, which after the first run makes them RETURNING
    accounts -- a different, easier claim than the one this item makes. Each run of this
    test needs an identity nobody has ever signed in as, so it mints one and deletes it
    when done rather than leaving it behind for the next run to inherit history from.
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

        username = f"newcomer-{secrets.token_hex(3)}"
        password = secrets.token_urlsafe(24)

        # firstName/lastName required, same reasoning as ensure-second-user.sh: without
        # them Keycloak's Verify Profile action halts the first login on a "complete your
        # account" form, which would make this test fail on Keycloak's own onboarding
        # rather than on anything this item is about.
        created = httpx.post(
            f"{idp}/admin/realms/{REALM}/users", headers=headers,
            json={"username": username, "email": f"{username}@example.invalid",
                  "firstName": "New", "lastName": "Comer",
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


@pytest.fixture(scope="module")
def browser():
    import os
    os.makedirs(SHOTS, exist_ok=True)
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


def _chat_frame(page, base_url, timeout=60):
    """The chat iframe specifically, not the portal shell that hosts it.

    `base_url in frame.url` is not enough: the shell page itself is served AT base_url
    (`.../portal/`), so a plain substring match finds the outer document, not the frame
    embedded inside it. The chat frame is the one that starts with base_url and is
    neither the portal shell nor the workshop.
    """
    end = time.time() + timeout
    while time.time() < end:
        for f in page.frames:
            u = f.url or ""
            if u.startswith(base_url) and "/portal" not in u and "/workshop" not in u:
                return f
        page.wait_for_timeout(500)
    raise AssertionError(f"no chat frame under {base_url!r} after {timeout}s")


def _fetch_persisted_message(chat, conversation_id, timeout_s=60):
    """Read the persisted assistant message back from the surface's own API, from
    INSIDE the chat frame's own JS context -- so it rides the browser's real session
    (the httpOnly refresh cookie) exactly the way the SPA itself does, rather than this
    test trying to reconstruct a separate authenticated HTTP client.

    Mirrors `tests/chat_turn.py::wait_for_reply` (refresh for a bearer token, then
    `GET /api/messages/<id>`, poll until the last non-user message is no longer
    `unfinished`) but runs as page JavaScript because that is the only place this
    browser-driven test holds a live, cookie-backed session.
    """
    result = chat.evaluate(
        """
        async ({ conversationId, timeoutMs }) => {
          const refreshResp = await fetch('/api/auth/refresh', {
            method: 'POST', credentials: 'include',
          });
          if (!refreshResp.ok) {
            return { error: `refresh failed: ${refreshResp.status}` };
          }
          const { token } = await refreshResp.json();
          if (!token) {
            return { error: 'refresh response carried no token' };
          }
          const deadline = Date.now() + timeoutMs;
          let last = null;
          while (Date.now() < deadline) {
            const r = await fetch('/api/messages/' + conversationId, {
              headers: { Authorization: 'Bearer ' + token },
              credentials: 'include',
            });
            if (r.ok) {
              last = await r.json();
              const replies = (last || []).filter(m => !m.isCreatedByUser);
              if (replies.length && !replies[replies.length - 1].unfinished) {
                return { message: replies[replies.length - 1] };
              }
            }
            await new Promise(res => setTimeout(res, 2000));
          }
          return { error: 'no finished assistant reply persisted', last };
        }
        """,
        {"conversationId": conversation_id, "timeoutMs": timeout_s * 1000},
    )
    if result.get("error"):
        pytest.fail(
            f"could not read the persisted message for conversation {conversation_id}: "
            f"{result['error']} -- last read: {json.dumps(result.get('last'))[:500]}"
        )
    return result["message"]


def test_a_never_signed_in_account_reaches_a_working_first_conversation(
        browser, base_url, fresh_account):
    """The item, driven exactly the way its DONE condition says to measure it."""
    username, password = fresh_account
    ctx = browser.new_context(ignore_https_errors=True, viewport={"width": 1440, "height": 900})
    page = ctx.new_page()
    console_errors = []
    page.on("console",
            lambda m: console_errors.append(m.text) if m.type == "error" else None)

    # Captured from the network rather than guessed: the POST that starts the turn
    # answers `{"conversationId": ..., "streamId": ..., "status": "started"}` on the
    # pinned v0.8.7 image (tests/chat_turn.py's own docstring). Listening for it is how
    # this test gets a real conversation id without adding any client-side plumbing of
    # its own -- it is exactly what the browser already receives.
    captured = {}

    def _on_response(resp):
        if "/api/agents/chat/" in resp.url and resp.request.method == "POST":
            try:
                body = resp.json()
            except Exception:
                return
            cid = body.get("conversationId")
            if cid:
                captured["conversation_id"] = cid

    page.on("response", _on_response)

    try:
        # The one thing this account is told: the documented front door
        # (deploy/README.md), trailing slash included -- the exact form this item's own
        # constraint text names as something a user has had to be told about.
        page.goto(f"{base_url}/portal/", wait_until="load", timeout=60000)

        # A never-before-seen account gets a real Keycloak login form, not a bounce
        # through an existing session -- direct evidence nothing about this identity was
        # pre-provisioned.
        page.wait_for_selector("input[name='username']", timeout=30000)
        page.fill("input[name='username']", username)
        page.fill("input[name='password']", password)
        page.click("input[type='submit'], button[type='submit']")
        page.wait_for_load_state("load", timeout=60000)

        # No instruction on which tab to use: the shell must open on Chat by itself.
        page.wait_for_selector("#tab-chat", timeout=30000)
        assert page.get_attribute("#tab-chat", "aria-selected") == "true", (
            "a brand-new account did not land on the Chat tab by default -- the first "
            "thing they would need is somebody telling them which tab to click"
        )
        page.screenshot(path=f"{SHOTS}/222-01-landed.png")

        chat = _chat_frame(page, base_url, timeout=45)

        # The surface embedded in the tab must not ask this account to sign in a second
        # time -- one login reaching every surface is scope item 1, re-checked here for
        # an identity that has never exercised that path before.
        deadline = time.time() + 60
        body_text = ""
        while time.time() < deadline:
            try:
                body_text = chat.evaluate("() => document.body ? document.body.innerText : ''")
            except Exception:
                body_text = ""
            if body_text and "Sign in with" not in body_text:
                break
            page.wait_for_timeout(1500)
        assert "Sign in with" not in body_text, (
            "chat asked a brand-new, already-portal-authenticated account to sign in again"
        )

        # Type a real prompt into the real composer. No tool named, no model chosen, no
        # toggle touched -- the default model spec (librechat.yaml modelSpecs, default:
        # true / prioritize: true) and its `webSearch: true` preset
        # (enterpriseaiframework-222) are what are supposed to make this work unaided.
        chat.wait_for_selector("#prompt-textarea", timeout=30000)
        chat.fill("#prompt-textarea", PROMPT)
        # `no_wait_after=True`: sending the first message in a brand-new conversation
        # triggers a client-side (pushState) route change to `/c/<conversationId>`, which
        # Playwright's default post-click "wait for navigation" heuristic can mistake for
        # an in-flight full navigation and hang on until its own timeout even though the
        # click has already fired and the turn has already started -- observed directly:
        # the frame's URL had already updated to the new conversation before the bare
        # `.click()` call timed out. Not a product behaviour change; every subsequent wait
        # below still asserts on the real end state.
        chat.click('[data-testid="send-button"]', no_wait_after=True)
        page.screenshot(path=f"{SHOTS}/222-02-sent.png")

        # A completed assistant turn renders a copy-response control. Waiting on that,
        # rather than a fixed sleep, is what makes this assert on whether an answer
        # actually arrived rather than on how fast the model happened to run this time.
        # NOTE: by itself this proves only that SOME message finished -- see the
        # tool_call assertion below, which is the actual done-condition check.
        chat.wait_for_selector('[data-testid="copy-response-button"]',
                              timeout=TURN_BUDGET_S * 1000)
        reply = chat.evaluate("() => document.body.innerText")
        page.screenshot(path=f"{SHOTS}/222-03-reply.png", full_page=True)

        # This prompt was chosen so the first turn never needs code execution, and this
        # test never opens "Your shared work" -- so a first-conversation failure here
        # cannot be enterpriseaiframework-c8b or -ce2 wearing a different name. If either
        # signature shows up anyway, that is itself the finding.
        lower = reply.lower()
        for bad in ("enotfound codeapi", "request to http://codeapi",
                    "something went wrong", "an error occurred"):
            assert bad not in lower, f"the first turn surfaced an error signature: {bad!r}"

        # THE NEGATIVE CONTROL (item's own done-condition requirement). A model that
        # writes the tool call out as prose still finishes a normal-looking message with
        # a copy button -- the assertion above passes either way. This is what catches
        # exactly that failure, using the literal signature measured on the live
        # cluster 2026-07-31.
        for sig in LEAKED_TOOL_CALL_SIGNATURES:
            assert sig not in reply, (
                f"the assistant's reply contains {sig!r} -- this is the tool call being "
                f"written out as PROSE instead of actually being invoked (the defect "
                f"enterpriseaiframework-222 fixes), not a grounded answer. Full reply: "
                f"{reply!r}"
            )

        # THE POSITIVE CHECK THAT ACTUALLY DISTINGUISHES A REAL ANSWER FROM A FABRICATED
        # ONE. `[data-testid="copy-response-button"]` and "no error string in the DOM"
        # both also pass for a message whose entire content is a plain-text tool-call
        # leak or a confidently fabricated answer from parametric memory -- that was the
        # prior wave's whole defect. A `tool_call` content block on the PERSISTED message
        # is created by LibreChat only when a tool was actually invoked through the
        # model's native tool-calling path; no code path fabricates one from text, so its
        # presence is real evidence the turn was grounded rather than guessed or leaked.
        assert "conversation_id" in captured, (
            "never observed a conversationId on the network -- the POST that starts a "
            "turn (`/api/agents/chat/<endpoint>`) either never fired or its response "
            "shape changed; see tests/chat_turn.py's docstring for the two protocols "
            "this surface has used"
        )
        message = _fetch_persisted_message(chat, captured["conversation_id"],
                                            timeout_s=TURN_BUDGET_S)
        tool_calls = [
            b for b in (message.get("content") or [])
            if isinstance(b, dict) and b.get("type") == "tool_call"
        ]
        web_search_calls = [
            c for c in tool_calls
            if "web_search" in ((c.get("tool_call") or {}).get("name") or "")
        ]
        assert web_search_calls, (
            "the persisted assistant message carries no real web_search tool_call block "
            f"-- got content types {[b.get('type') for b in (message.get('content') or [])]!r}. "
            "A never-signed-in account's first turn must be grounded in a tool call that "
            "ACTUALLY EXECUTED, not answered from parametric memory or faked as text. "
            f"Full message: {json.dumps(message)[:2000]}"
        )

        assert not console_errors, (
            f"console errors during a brand-new account's first conversation: {console_errors}"
        )
    finally:
        ctx.close()
