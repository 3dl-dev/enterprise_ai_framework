"""A whole portal-plus-workshop stack on loopback, for browser tests that must not touch
a real person's workspace.

WHY THIS EXISTS

The browser tests in this directory sign in to the live cluster as a real account, and the
Code tab drives that account's own pod: switching to it starts a session, writes
`.meta/<project>.session`, and clears the new-session flag. Running a test that way
mutates the state of whoever owns the pod. There is no acceptable version of that — a test
must never be able to convert a user's session, and "we only looked" is not true of the
Code tab.

So the portal is hosted here instead, against a throwaway projects directory, and the only
person who can be inconvenienced by a run is the person doing the run.

WHAT IS REAL AND WHAT IS NOT

Real, exercised as shipped:
  * `control-plane/app/portal_static/{index.html,app.js,style.css}` — the actual page.
  * `app.portal.require_user` — identity is derived by the shipped predicate, from a header
    an in-process hop injects the way oauth2-proxy does, over loopback.
  * `app.portal.me` — the links payload the account menu is wired from, built by the
    shipped code from the configured PUBLIC_BASE_URL and IDP_REALM.
  * `app.portal.my_spend`, `my_keys`, `my_published`, `rotate_my_key` — the real handlers
    behind the settings sheet, including the filter that decides which rows are yours.
  * `app.chat_identity` — the real module, its regex and its resolve(), with only the Mongo
    read replaced (see below). This is the path whose docstring warns that filtering before
    translating silently drops every chat row from a user's own total.
  * `app.workshop.workshop_proxy` — the real proxy, including its token header and its one
    `<base href>` rewrite.
  * `deploy/workspace/shell-server.py` — the real workshop shell, as a subprocess, over a
    throwaway WS_PROJECTS_ROOT (same pattern as tests/test_workspace_shell.py).

Stubbed, and why:
  * `_host()` — off-cluster there is no `ws-<user>` DNS name to resolve. It is redirected
    to loopback FOR THE EXPECTED USER ONLY, so "the upstream comes from the authenticated
    name" is still live: any other name still resolves to `ws-<name>` and fails.
  * ttyd — a page that serves one static document on the ttyd port. ttyd is not what this
    stack is for, and the shell nests it in a further iframe.
  * chat, the account console, and the published site — three marker pages, because what
    is under test is whether the portal's links arrive at the right URL, not what the thing
    at the other end renders. They are served ONLY at the exact paths the shipped code is
    supposed to emit, so a link naming the wrong realm or the wrong user gets a 404 rather
    than a page.
  * The four data sources the settings handlers read — `metering.spend_by_user_and_surface`
    (Postgres), `gateway.list_keys` (LiteLLM's admin API), `chat_identity`'s Mongo lookup,
    and the published volume's directory listing (an HTTP GET the real handler makes, so
    that one is served rather than patched). They are replaced at the OUTER boundary only:
    every line of this repo's own filtering, translating, projecting and sorting still
    runs. The fixtures deliberately contain a second account's rows and keys, a chat row
    keyed by a LibreChat ObjectId, and a key carrying a `token` field — so the tests can
    assert what must NOT reach the page, which a live deployment's data cannot guarantee.

No cluster, no docker, no bundle, no credential, no network.
"""

import asyncio
import json
import mimetypes
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
CONTROL_PLANE = ROOT / "control-plane"
SHELL_SERVER = ROOT / "deploy" / "workspace" / "shell-server.py"

USER = "harness-user"
# A second account that exists only in the fixtures. Nothing signs in as it; it is here so
# that "this row is mine" can be distinguished from "a row exists", which is the difference
# between a presence check and a correctness check.
OTHER_USER = "harness-other"
REALM = "harness-realm"
PROJECT = "rocket"
# A 24-character hex ObjectId, which is what LibreChat forwards as `end_user` and what
# chat_identity's real regex is written to recognise. The chat row below is keyed on it, so
# a regression that filtered before translating would drop that row and the total with it.
CHAT_ID = "6a67b18069dba4d1126fef44"
PUBLISHED_PROJECT = "rocket"
# Local to this process and never a cluster credential: the shared per-deployment token
# is the same string in every pod, so a test has no business holding one, let alone
# printing one.
TOKEN = "harness-" + os.urandom(8).hex()

# ---------------------------------------------------------------- the fixture data
#
# The shape `metering.spend_by_user_and_surface` returns: one row per (principal, surface),
# where the principal is a key-alias owner for every surface except chat, whose principal is
# LibreChat's own primary key.
SPEND_ROWS = [
    {"username": USER, "surface": "ide", "requests": 12, "spend": 0.004212,
     "prompt_tokens": 8100, "completion_tokens": 2300},
    {"username": CHAT_ID, "surface": "chat", "requests": 5, "spend": 0.000915,
     "prompt_tokens": 1200, "completion_tokens": 640},
    # Zero spend, non-zero requests: a surface that was used and cost nothing must still be
    # a row. A filter written as `if not spend: continue` would drop it.
    {"username": USER, "surface": "terminal", "requests": 2, "spend": 0.0,
     "prompt_tokens": 40, "completion_tokens": 0},
    # Somebody else's, and much larger, so that leaking it into the signed-in user's total
    # is impossible to miss.
    {"username": OTHER_USER, "surface": "ide", "requests": 99, "spend": 7.5,
     "prompt_tokens": 500000, "completion_tokens": 90000},
]

#: The total the shipped handler must compute for USER: 0.004212 + 0.000915 + 0.0.
SPEND_TOTAL_RENDERED = "$0.00513"
#: In the order app.js renders them — the handler sorts by descending spend.
SPEND_SURFACES_RENDERED = ["ide", "chat", "terminal"]

#: What `gateway.list_keys` returns. `token` is present on one of them on purpose: the
#: shipped projection is supposed to drop it, and "no secret reached the page" is only a
#: real assertion if there was a secret available to leak.
GATEWAY_KEYS = [
    {"key_alias": f"{USER}::ide", "max_budget": 5.0, "spend": 0.004212,
     "created_at": "2026-07-01T00:00:00Z", "token": "sk-harness-ide-must-not-be-served"},
    {"key_alias": f"{USER}::chat", "max_budget": None, "spend": 0.000915,
     "created_at": "2026-07-01T00:00:00Z"},
    {"key_alias": f"{OTHER_USER}::ide", "max_budget": 5.0, "spend": 7.5,
     "created_at": "2026-07-01T00:00:00Z"},
    # A shared surface key, which is not a person and has no `owner::surface` shape at all.
    {"key_alias": "chat-surface", "max_budget": None, "spend": 1.25,
     "created_at": "2026-07-01T00:00:00Z"},
]

#: In the order app.js renders them — the handler sorts by surface name.
KEY_ALIASES_RENDERED = [f"{USER}::chat", f"{USER}::ide"]

#: What the published volume's listing endpoint answers for USER. The `file` entry is here
#: because the shipped handler is supposed to keep directories only.
PUBLISHED_LISTING = [
    {"type": "directory", "name": PUBLISHED_PROJECT, "mtime": "2026-07-28T10:00:00Z"},
    {"type": "file", "name": "index.html", "mtime": "2026-07-28T10:00:00Z"},
]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Stack:
    """The stack, wired once per process because the shipped modules read their ports and
    URLs from the environment at import time."""

    def __init__(self):
        self.port = _free_port()
        self.shell_port = _free_port()
        self.ttyd_port = _free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"

        os.environ["PUBLIC_BASE_URL"] = self.base_url
        os.environ["IDP_REALM"] = REALM
        os.environ["WORKSPACE_INTERNAL_TOKEN"] = TOKEN
        os.environ["WORKSPACE_SHELL_PORT"] = str(self.shell_port)
        os.environ["WORKSPACE_TTYD_PORT"] = str(self.ttyd_port)
        # The published volume's listing service, which the shipped my_published handler
        # fetches over HTTP. Pointed at this harness so that request really happens rather
        # than being patched out.
        os.environ["PUBLISHED_INTERNAL_URL"] = self.base_url
        # No operator, so the admin panel must stay hidden and its endpoints must 404. The
        # shipped code reads this at import; setting it explicitly rather than relying on
        # the default keeps the expectation in one place.
        os.environ["PORTAL_ADMINS"] = ""
        # chat_identity would otherwise try to reach a Mongo somewhere.
        os.environ["CHAT_MONGO_URL"] = ""

        #: Every rotation the page asked for. Nothing in this suite is supposed to rotate a
        #: key, so a test asserts this stays empty — the live version of that test could
        #: only hope.
        self.rotations: list[tuple[str, str]] = []

        self.portal, self.workshop = _import_shipped_modules(self)
        # The one host stub. Still derived from identity: only the authenticated name maps
        # to loopback, so a request that somehow arrived as somebody else still leaves for
        # a `ws-<them>` that does not exist.
        self.workshop._host = lambda user: "127.0.0.1" if user == USER else f"ws-{user}"

        self._shell_proc: subprocess.Popen | None = None
        self._impostor: ThreadingHTTPServer | None = None
        self._servers: list[ThreadingHTTPServer] = []

    # ------------------------------------------------------------------ servers

    def start(self):
        self._serve(self.ttyd_port, _make_ttyd_stub())
        self._serve(self.port, _make_portal_handler(self))
        _wait_for(f"{self.base_url}/health-harness", "the harness")

    def _serve(self, port: int, handler):
        srv = ThreadingHTTPServer(("127.0.0.1", port), handler)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self._servers.append(srv)

    def stop(self):
        self.stop_shell()
        for srv in self._servers:
            srv.shutdown()
            srv.server_close()
        self._servers.clear()

    # ------------------------------------------------------------------ the workshop

    def start_shell(self, projects_root: Path):
        """Run the real workshop shell over a throwaway projects directory."""
        (projects_root / PROJECT).mkdir(parents=True, exist_ok=True)
        (projects_root / ".active").write_text(PROJECT + "\n")
        env = {
            **os.environ,
            "WS_PROJECTS_ROOT": str(projects_root),
            "WS_SHELL_PORT": str(self.shell_port),
            "WS_PULSE_INTERVAL": "0.2",
            "WS_USER": USER,
            "WS_PUBLISH_URL": self.base_url,
            "WS_INTERNAL_TOKEN": TOKEN,
        }
        self._shell_proc = subprocess.Popen(
            [sys.executable, str(SHELL_SERVER)], env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        _wait_for(f"http://127.0.0.1:{self.shell_port}/api/state", "the workshop shell",
                  headers={"X-Workspace-Token": TOKEN}, proc=self._shell_proc)

    def start_impostor(self):
        """Answer 200 on the workshop's port with something that is not the workshop.

        The failure that matters is not an error status. It is a 200 that is not the thing
        asked for — an authenticating proxy in front of the pod serving its own sign-in
        page, a stale index, another tenant's document. Nothing in the browser reports it:
        no failed request, no console error, and the iframe's rectangle in the parent
        document is identical. Only an assertion about the frame's CONTENT can see it.
        """
        body = _page("Sign in", "impostor-signin", "Sign in to continue")

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        srv = ThreadingHTTPServer(("127.0.0.1", self.shell_port), Handler)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self._impostor = srv

    def stop_shell(self):
        if self._shell_proc:
            self._shell_proc.kill()
            self._shell_proc.wait(timeout=10)
            self._shell_proc = None
        if getattr(self, "_impostor", None):
            self._impostor.shutdown()
            self._impostor.server_close()
            self._impostor = None

    @property
    def shell_running(self) -> bool:
        return self._shell_proc is not None and self._shell_proc.poll() is None

    # ------------------------------------------------------------------ expectations

    @property
    def account_url(self) -> str:
        return f"{self.base_url}/realms/{REALM}/account"

    @property
    def password_url(self) -> str:
        """Where the account menu item is supposed to land: the console's security
        section, not merely the console. The shipped page prefers `links.password`."""
        return f"{self.account_url}#/security/signingin"

    @property
    def published_url(self) -> str:
        return f"{self.base_url}/live/{USER}/"


def _import_shipped_modules(stack: "_Stack"):
    """Import app.portal and app.workshop, with their four data sources stood in for.

    Same trade as control-plane/tests/test_portal_auth.py for the import itself: `app.db`
    wants a live Postgres at import time and nothing here goes near it.

    What is different from the first version of this harness is WHERE the line is drawn.
    `app.metering` and `app.gateway` are not empty modules any more — they answer the one
    query each that the settings sheet reaches, with the fixtures at the top of this file.
    Everything between those answers and the browser is the shipped code: the per-user
    filter, the chat-id translation, the aggregation, the projection that drops key
    secrets, and both sort orders. Stubbing `my_spend` itself would have tested the page
    and nothing else; stubbing the SQL tests the page and the handler.

    `app.chat_identity` is imported for real — it has no database dependency of its own,
    only an optional pymongo one — and its cache is seeded directly. So the regex that
    decides what looks like a chat id, and resolve()'s fall-through for anything that does
    not, are the shipped ones.
    """
    import types

    if str(CONTROL_PLANE) not in sys.path:
        sys.path.insert(0, str(CONTROL_PLANE))
    import app  # noqa: F401  (the real package; __init__ is empty)

    # app.db is the only one that must not be imported at all: it opens a pool.
    if "app.db" not in sys.modules:
        sys.modules["app.db"] = types.ModuleType("app.db")

    metering = sys.modules.setdefault("app.metering", types.ModuleType("app.metering"))
    gateway = sys.modules.setdefault("app.gateway", types.ModuleType("app.gateway"))
    issuance = sys.modules.setdefault("app.issuance", types.ModuleType("app.issuance"))

    async def spend_by_user_and_surface(since: str | None = None):
        # `since` is accepted and ignored: the page's default is no window, and a window
        # filter belongs to the SQL this stands in for. A test that changed the selector
        # would be testing this function, not the product.
        return [dict(r) for r in SPEND_ROWS]

    async def list_keys():
        return [dict(k) for k in GATEWAY_KEYS]

    async def issue(user: str, surface: str, actor: str | None = None):
        stack.rotations.append((user, surface))
        return {"key_alias": f"{user}::{surface}", "key": "sk-harness-rotated",
                "rotated": True}

    metering.spend_by_user_and_surface = spend_by_user_and_surface
    gateway.list_keys = list_keys
    gateway.SURFACES = ("chat", "ide", "terminal")
    issuance.issue = issue

    # The real module, with only the Mongo read stood in for.
    from app import chat_identity
    chat_identity._cache = {CHAT_ID: USER}
    chat_identity._loaded = True

    from app import portal, workshop
    return portal, workshop


def _wait_for(url: str, what: str, headers: dict | None = None,
              proc: subprocess.Popen | None = None, timeout: float = 20.0):
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            err = proc.stderr.read().decode() if proc.stderr else ""
            raise RuntimeError(f"{what} exited early: {err[:2000]}")
        try:
            r = httpx.get(url, headers=headers or {}, timeout=5.0)
            if r.status_code < 500:
                return
            last = f"{r.status_code}"
        except httpx.HTTPError as exc:
            last = type(exc).__name__
        time.sleep(0.05)
    raise RuntimeError(f"{what} never came up ({last})")


# ---------------------------------------------------------------- the ttyd stub


def _make_ttyd_stub():
    body = (b"<!doctype html><meta charset=utf-8><title>terminal (stub)</title>"
            b"<body style='background:#12141a;color:#ddd;font:14px monospace'>"
            b"<div id='ttyd-stub'>terminal stub</div>")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    return Handler


# ---------------------------------------------------------------- the portal front door


def _query_one(query: str, name: str) -> str | None:
    """One query parameter, the way FastAPI would hand it to the handler."""
    from urllib.parse import parse_qs

    values = parse_qs(query).get(name) or []
    return values[0] if values else None


def _page(title: str, marker_id: str, text: str) -> bytes:
    return (f"<!doctype html><meta charset=utf-8><title>{title}</title>"
            f"<body style='font:16px system-ui;padding:2rem'>"
            f"<h1 id='{marker_id}'>{text}</h1>").encode()


def _make_portal_handler(stack: "_Stack"):
    from fastapi import HTTPException
    from fastapi.responses import FileResponse
    from starlette.requests import Request

    portal, workshop = stack.portal, stack.workshop

    STUBS = {
        # LibreChat's origin root. The portal points the Chat tab at PUBLIC_BASE_URL.
        "/": _page("Chat (stub)", "chat-stub", "chat surface"),
        # Keycloak's account console, at the ONE path the shipped code should emit. A
        # stale or wrong-realm URL lands on nothing and the test sees a 404.
        f"/realms/{REALM}/account": _page("Account console (stub)", "kc-account",
                                          f"account console for {REALM}"),
        # This user's published site, again at the one correct path.
        f"/live/{USER}/": _page("Your shared work (stub)", "published-stub",
                                f"published work of {USER}"),
    }

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            self._handle("GET")

        def do_POST(self):
            self._handle("POST")

        def _handle(self, method: str):
            path, _, query = self.path.partition("?")
            if path == "/health-harness":
                self._send(200, b"ok", "text/plain")
                return
            # The published volume's directory listing. Answered before require_user
            # because this request comes from the shipped my_published handler's own httpx
            # client, not from the browser, and carries no identity header — exactly as it
            # does in the cluster, where `published` is a separate service.
            if method == "GET" and path.startswith("/listing/"):
                wanted = path[len("/listing/"):].strip("/")
                body = json.dumps(PUBLISHED_LISTING if wanted == USER else []).encode()
                self._send(200, body, "application/json")
                return
            if method == "GET" and path in STUBS:
                self._send(200, STUBS[path], "text/html; charset=utf-8")
                return
            try:
                asyncio.run(self._route(method, path, query))
            except HTTPException as exc:
                # What FastAPI's own handler would have produced, which is what the
                # browser sees in production when a workshop is not up.
                self._send(exc.status_code, json.dumps({"detail": exc.detail}).encode(),
                           "application/json")
            except Exception as exc:  # pragma: no cover - harness failure, not product
                self._send(500, f"harness error: {exc!r}".encode(), "text/plain")

        async def _route(self, method: str, path: str, query: str):
            request = self._as_request(method, path, query)
            # The oauth2-proxy hop, done by the shipped predicate rather than around it.
            user = portal.require_user(request)

            if path in ("/portal", "/portal/"):
                self._send_response(await portal.portal_index(user=user), FileResponse)
            elif path.startswith("/portal/static/"):
                name = path[len("/portal/static/"):]
                self._send_response(await portal.portal_static(name, user=user),
                                    FileResponse)
            elif path == "/portal/api/me":
                self._send(200, json.dumps(await portal.me(request, user=user)).encode(),
                           "application/json")
            elif path == "/portal/api/spend":
                since = _query_one(query, "since")
                self._send(200, json.dumps(await portal.my_spend(since, user=user)).encode(),
                           "application/json")
            elif path == "/portal/api/keys":
                self._send(200, json.dumps(await portal.my_keys(user=user)).encode(),
                           "application/json")
            elif path == "/portal/api/published":
                self._send(200, json.dumps(await portal.my_published(user=user)).encode(),
                           "application/json")
            elif path == "/portal/api/keys/rotate" and method == "POST":
                body = json.loads(await request.body() or b"{}")
                self._send(200,
                           json.dumps(await portal.rotate_my_key(body, user=user)).encode(),
                           "application/json")
            elif path.startswith("/portal/api/admin/"):
                # The shipped predicate decides. PORTAL_ADMINS is empty here, so this is
                # the 404 a non-operator is supposed to get — and the page must not render
                # the panel on the strength of it.
                portal.require_admin_user(request, user=user)
                self._send(500, b"unreachable: require_admin_user admitted a non-operator",
                           "text/plain")
            elif path == "/workshop" or path.startswith("/workshop/"):
                sub = path[len("/workshop/"):] if path.startswith("/workshop/") else ""
                resp = await workshop.workshop_proxy(sub, request, user=user)
                self._send(resp.status_code, resp.body,
                           resp.headers.get("content-type", "application/octet-stream"))
            else:
                self._send(404, b"not found", "text/plain")

        def _as_request(self, method: str, path: str, query: str) -> Request:
            headers = [(k.lower().encode(), v.encode()) for k, v in self.headers.items()]
            # The identity header oauth2-proxy sets. Injected here, on the hop in front of
            # the control plane, exactly where the sidecar sits.
            headers.append((b"x-auth-request-preferred-username", USER.encode()))
            headers.append((b"x-auth-request-email", f"{USER}@example.invalid".encode()))
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""

            async def receive():
                return {"type": "http.request", "body": body, "more_body": False}

            return Request({
                "type": "http", "http_version": "1.1", "method": method,
                "scheme": "http", "path": path, "raw_path": path.encode(),
                "query_string": query.encode(), "headers": headers,
                # Loopback, because that is where the sidecar's requests come from and
                # require_user is entitled to insist on it.
                "client": ("127.0.0.1", 0), "server": ("127.0.0.1", stack.port),
            }, receive)

        def _send_response(self, resp, FileResponseType):
            if isinstance(resp, FileResponseType):
                data = Path(resp.path).read_bytes()
                ctype = resp.media_type or mimetypes.guess_type(resp.path)[0] \
                    or "application/octet-stream"
                self._send(200, data, ctype)
            else:  # pragma: no cover - the shipped code returns FileResponse today
                self._send(resp.status_code, resp.body, resp.media_type or "text/plain")

        def _send(self, status: int, body: bytes, ctype: str):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    return Handler


_STACK: _Stack | None = None


def stack() -> _Stack:
    """The process-wide stack. Started on first use; the modules under test read their
    configuration from the environment at import, so there can only be one."""
    global _STACK
    if _STACK is None:
        s = _Stack()
        s.start()
        _STACK = s
    return _STACK


def shutdown():
    global _STACK
    if _STACK is not None:
        _STACK.stop()
        _STACK = None
