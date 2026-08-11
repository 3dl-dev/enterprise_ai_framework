#!/usr/bin/env python3
"""The workspace shell: serves the UI, the live preview, and a small action API.

WHY THIS EXISTS

A bare browser terminal is not a product. A child needs to see the thing they are
building, be told what to try, and get a link they can send to a parent — without
learning a shell first. Nothing off-the-shelf is shaped like that, so this is the one
piece of the surface we write ourselves. It is deliberately tiny: stdlib only, no build
step, no framework, no dependencies to age.

WHAT IT DOES NOT DO

It does not authenticate. oauth2-proxy sits in front of every route in this pod and has
already established who the user is before anything reaches here. It does not talk to the
cluster, hold a cluster credential, or know any other user exists. Its whole world is this
pod's own project directory.

ROUTES (all behind oauth2-proxy)
  /               the shell UI
  /preview/       the project directory, served raw so the child sees their own app
  /api/state      what exists right now: has an index.html, is it published, which model
  /api/pulse      what is happening right now: revision, what changed, is the agent working
  /api/publish    run `publish`, return the parent-facing link
  /api/model      read/write the agent's model
  /api/projects   create a project and switch to it
  /api/switch     change the active project
  /api/reset      empty the active project, keeping its git history
  /api/delete     remove a project and anything it published
  /terminal/      NOT handled here — oauth2-proxy routes it straight to ttyd
"""

import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJECTS_ROOT = Path(os.environ.get("WS_PROJECTS_ROOT", "/workspace/projects"))
ACTIVE_FILE = PROJECTS_ROOT / ".active"
USER = os.environ.get("WS_USER", "coder")
PUBLISH_URL = os.environ.get("WS_PUBLISH_URL", "")
PUBLISHED_ROOT = Path("/published") / USER

# Publish bookkeeping lives OUTSIDE any project directory, so the agent never sees it,
# never edits it, and it is never published or committed alongside the child's work.
META_ROOT = PROJECTS_ROOT / ".meta"

# Project names become directory names and URL segments, so they are constrained rather
# than sanitised — a rejected name is easy to explain, a silently rewritten one is not.
SLUG_OK = re.compile(r"^[a-z0-9][a-z0-9-]{0,38}$")


# Tidying is for how a child types — "Space Cats!!" should become "space-cats". It is NOT
# for path-ish input: "../escape" must be refused outright, not quietly turned into
# "escape". Anything with a separator or a dot run is rejected before tidying happens.
UNSAFE = re.compile(r"[/\\]|\.\.")


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9-]+", "-", name.strip().lower()).strip("-")
    return re.sub(r"-{2,}", "-", s)[:39]


def active_project() -> str:
    # Hidden directories are NOT projects, and the filter is load-bearing rather than
    # tidy: .meta lives in this root, "." sorts before every letter and digit, so an
    # unfiltered sort picks it first. The one time this fallback runs — .active naming a
    # directory that is gone — the workshop would silently adopt ".meta" as the active
    # project, show a project name that is not in its own menu, serve the publish
    # bookkeeping through /preview, and let "Start over" empty it. list_projects() has
    # always filtered these; this must agree with it.
    try:
        name = ACTIVE_FILE.read_text().strip()
        if name and not name.startswith(".") and (PROJECTS_ROOT / name).is_dir():
            return name
    except OSError:
        pass
    existing = sorted(p.name for p in PROJECTS_ROOT.glob("*")
                      if p.is_dir() and not p.name.startswith("."))
    return existing[0] if existing else "my-first-project"


def project_dir() -> Path:
    return PROJECTS_ROOT / active_project()


def list_projects() -> list[dict]:
    out = []
    for d in sorted(PROJECTS_ROOT.glob("*")):
        if not d.is_dir() or d.name.startswith("."):
            continue
        out.append({
            "name": d.name,
            "has_index": (d / "index.html").is_file(),
            "published": (PUBLISHED_ROOT / d.name).is_dir(),
        })
    return out
STATIC = Path(__file__).resolve().parent / "shell"
PORT = int(os.environ.get("WS_SHELL_PORT", "7682"))

# The credential the control plane presents. See entrypoint.sh for why this pod is no
# longer loopback-only and what the two replacement controls are. Empty means refuse
# everything rather than allow everything — an unauthenticated shell API on the pod
# network is worse than a workshop that will not load.
INTERNAL_TOKEN = os.environ.get("WS_INTERNAL_TOKEN", "")

# Offered in Settings. GLM 5.2 stays first so it remains the default a new project starts
# on — switching is opt-in and per-project, so adding alternates here never touches a
# running session. Every id MUST also be declared in opencode.json's
# `provider.enterprise-ai.models` block with its real context and output limits, or
# opencode cannot resolve it. Kept in step with the gateway catalogue.
MODELS = [
    {"id": "glm-5.2@deepinfra", "label": "GLM 5.2 — better at hard things"},
    {"id": "glm-4.7@deepinfra", "label": "GLM 4.7 — faster and cheaper"},
    {"id": "qwen3-coder-480b-a35b-instruct@deepinfra",
     "label": "Qwen3 Coder — fast, coding-tuned, no reasoning stall"},
    {"id": "deepseek-v4-pro@deepinfra",
     "label": "DeepSeek V4 Pro — hardest tasks, 1M context"},
    {"id": "deepseek-v4-flash@deepinfra",
     "label": "DeepSeek V4 Flash — cheapest, huge context"},
]


def _agent_model() -> str:
    cfg = project_dir() / "opencode.json"
    if cfg.exists():
        try:
            model = json.loads(cfg.read_text()).get("model", "")
            return model.split("/", 1)[-1] if model else MODELS[0]["id"]
        except (json.JSONDecodeError, OSError):
            pass
    return MODELS[0]["id"]


def _set_agent_model(model_id: str) -> None:
    """Write the model into the project config the agent reads when it starts.

    A model change also asks for a FRESH session, and that is not a preference — it is
    what opencode does. The model is pinned to the session, so a resumed one comes back on
    the model it was started with. Measured directly: `opencode --continue --model
    glm-4.7` paints "GLM 4.7" and then "GLM 5.2" as the session loads over it. Writing the
    config and reconnecting therefore changed nothing a user could see, which is exactly
    what "why doesn't the setting do anything" looked like.

    So switching models starts a new chat. The client says so before doing it. Files are
    untouched — this only ends the conversation, and a setting that silently does nothing
    is worse than one that tells you its price.
    """
    if model_id not in {m["id"] for m in MODELS}:
        raise ValueError(f"unknown model: {model_id}")
    cfg = project_dir() / "opencode.json"
    data = json.loads(cfg.read_text()) if cfg.exists() else {}
    data["model"] = f"enterprise-ai/{model_id}"
    cfg.write_text(json.dumps(data, indent=2) + "\n")


# ---------------------------------------------------------------------- pulse
#
# /api/pulse answers "what is happening right now" so the UI can narrate it. It is a
# 1 Hz POLL, deliberately, and there is no SSE endpoint. A server-sent stream is lower
# latency and fewer requests, but a buffered stream through oauth2-proxy fails as
# "nothing ever happens" — indistinguishable from a hung agent — whereas a poll fails as
# "slightly late". On the eve of shipping to a room of nine-year-olds, the boring
# transport wins.
#
# Everything here is computed by ONE background sampler on a fixed window, never
# per-request. That keeps the endpoint O(1) and — the real reason — makes `busy` depend
# on a known time window rather than on however often a particular browser happens to
# poll.

SCAN_CAP = 2000
SCAN_EXCLUDE = {".git", "node_modules", "__pycache__", ".meta"}
AGENT_NAMES = {"opencode", "aider"}

# The sampler's window. Overridable ONLY so the tests can run a fast clock; production
# leaves it at 1.0 and the busy threshold below is expressed as a rate so the meaning of
# "busy" does not change when the window does.
PULSE_INTERVAL = float(os.environ.get("WS_PULSE_INTERVAL", "1.0"))

# Jiffies per second of agent CPU that counts as working. opencode's TUI burns a trickle
# redrawing on a keystroke; real work is an order of magnitude above this.
BUSY_JIFFIES_PER_S = 6.0

# `changed` means "files in the current writing burst", not "the three newest files that
# exist". Without a recency window a project containing three old files would forever
# report three changed files, and the Ribbon would read "Writing 3 files…" while the
# agent sat idle. 4000 ms matches the window the Ribbon itself uses to decide the phrase.
CHANGED_WINDOW_MS = 4000

# No file has ever changed. Deliberately a large int rather than null: the client compares
# `last_change_ms < 4000`, and in JavaScript `null < 4000` is TRUE — a null here would
# claim a write that never happened.
NEVER_MS = 10 ** 9

# A reference the page fetches from somewhere other than itself.
#
# The name is historical and the comment that used to be here was wrong twice
# (enterpriseaiframework-644). It claimed "there is no egress except the AI gateway, so a
# model that emits a CDN <script src> produces a blank page." Both halves are false:
#
#   1. The workspace NetworkPolicy (deploy/k8s/60-workspace-common.yaml) has always allowed
#      egress to 0.0.0.0/0 minus the private ranges, on any port. Measured, not read:
#      `curl https://registry.npmjs.org/` from inside a running ws- pod returns 200.
#   2. It would not matter if it did not. The preview is an <iframe src="preview/"> — the
#      CHILD'S BROWSER fetches the page and every subresource in it, not this pod. This
#      server sends no Content-Security-Policy and the iframe's sandbox does not restrict
#      subresource loading, so a CDN <script src> in the preview loads from the child's own
#      connection. See test_the_preview_does_not_block_remote_subresources.
#
# The counter is still worth having, for the reason the house rules give: a remote
# reference is the one thing in a child's page that can work at the desk and be blank at
# the demo. It is a house-rule check, not a capability check. Renaming the field would
# change /api/pulse's frozen shape, so the name stays and this comment carries the truth.
OFFLINE_REF = re.compile(
    r"""<(?:script|link|img|source|iframe|audio|video)\b[^>]*?\b"""
    r"""(?:src|href)\s*=\s*["']?(?:https?:)?//""",
    re.IGNORECASE,
)


def _scan(root: Path) -> tuple[list[tuple[str, int, int]], bool]:
    """(relpath, mtime_ns, size) for every file under root, plus whether the cap was hit.

    os.scandir rather than os.walk because stat comes back with the directory entry, so a
    project is one syscall per file rather than two.
    """
    entries: list[tuple[str, int, int]] = []
    stack = [str(root)]
    while stack:
        try:
            it = os.scandir(stack.pop())
        except OSError:
            # The project can be deleted or reset underneath the sampler at any moment.
            # A vanished directory is a normal event here, not an error.
            continue
        with it:
            for e in it:
                if e.name in SCAN_EXCLUDE:
                    continue
                try:
                    if e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
                        continue
                    if not e.is_file(follow_symlinks=False):
                        continue
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                entries.append((os.path.relpath(e.path, root), st.st_mtime_ns, st.st_size))
                if len(entries) >= SCAN_CAP:
                    return sorted(entries), True
    return sorted(entries), False


def _fingerprint(entries: list[tuple[str, int, int]]) -> str:
    h = hashlib.sha1()
    for rel, mtime_ns, size in entries:
        h.update(f"{rel}\0{mtime_ns}\0{size}\0".encode())
    return h.hexdigest()


# Constructs that mean the page keeps running after it has loaded. A page that only
# paints once cannot make a tab unresponsive; a page holding an animation loop can, and
# when it does the browser shows its own "page isn't responding" dialog, which is a
# frightening way for a nine-year-old to learn their game has a bug in it.
#
# This is a HINT, not a verdict — it decides which button is emphasised, never whether
# something is allowed to run. A static page mislabelled costs one extra click; a heavy
# page mislabelled costs the tab.
LIVE_PAGE = re.compile(
    r"requestAnimationFrame|setInterval\s*\(|while\s*\(\s*(?:true|1)\s*\)"
    r"|<canvas|webkitRequestAnimationFrame",
    re.IGNORECASE,
)


def _scan_index(index: Path) -> tuple[int, bool]:
    """(offline_refs, keeps_running) from one read of index.html."""
    try:
        text = index.read_text(errors="replace")
    except OSError:
        return 0, False
    return len(OFFLINE_REF.findall(text)), bool(LIVE_PAGE.search(text))


def _offline_refs(index: Path) -> int:
    return _scan_index(index)[0]


def _has_published(project: str) -> bool:
    """Whether anything is sitting in this project's published slot.

    Wrapped because /published is a separate mounted volume that this process does not
    own. A permission problem there raised out of the sampler, and the sampler's first
    call happens during construction — so a mis-mounted publish volume stopped the whole
    shell from starting: no UI, no /api/state, no preview, on a pod whose terminal was
    fine. Losing the published flag costs one button label; losing the server costs the
    workshop.
    """
    pub = PUBLISHED_ROOT / project
    try:
        return pub.is_dir() and any(pub.iterdir())
    except OSError:
        return False


def _agent_jiffies() -> int | None:
    """Total CPU jiffies burned by the coding agent, or None if it cannot be observed.

    Deliberately NOT /proc/<pid>/io, which would be the obvious choice: it requires
    PTRACE_MODE_READ, and shell-server and opencode are siblings under entrypoint.sh
    rather than ancestor and descendant, so Yama ptrace_scope=1 (the Debian default)
    returns EACCES. /proc/<pid>/stat is world-readable and works.
    """
    try:
        pids = os.listdir("/proc")
    except OSError:
        return None
    uid = os.getuid()
    total = 0
    found = False
    for pid in pids:
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                argv = [a for a in fh.read().split(b"\0") if a]
            if not argv:
                continue
            name = os.path.basename(argv[0].decode("utf-8", "replace"))
            if name not in AGENT_NAMES:
                # opencode is a standalone binary, so argv[0] IS "opencode". aider is a
                # pip console script, so the kernel runs it through its shebang and argv
                # reads "/usr/local/bin/python /usr/local/bin/aider" — argv[0] is the
                # interpreter and the name never matched. Checked on the real layout:
                # a shebang script reports its interpreter, not itself. Without this the
                # entire WS_AGENT=aider path reports busy: null forever, which the Ribbon
                # renders as "cannot observe" — no writing phrases, no elapsed counter.
                if len(argv) < 2 or not name.startswith("python"):
                    continue
                if os.path.basename(argv[1].decode("utf-8", "replace")) not in AGENT_NAMES:
                    continue
            if os.stat(f"/proc/{pid}").st_uid != uid:
                continue
            with open(f"/proc/{pid}/stat") as fh:
                stat_line = fh.read()
            # The comm field is parenthesised AND may contain spaces and parens, so the
            # only safe split is after the LAST ") ". Index 0 is then field 3, which puts
            # utime at 11 and stime at 12. Get this wrong and everything downstream is wrong.
            fields = stat_line.rsplit(") ", 1)[1].split()
            total += int(fields[11]) + int(fields[12])
            found = True
        except (OSError, ValueError, IndexError):
            # The process exited between listdir and read. Normal, not an error.
            continue
    return total if found else None


class Pulse:
    """The 1 Hz sampler. One thread, one lock, one snapshot dict handed to every request."""

    def __init__(self):
        self._lock = threading.Lock()
        self._rev = 0
        self._fp: str | None = None
        self._project: str | None = None
        self._prev_jiffies: int | None = None
        self._over = 0
        self._under = 0
        self._busy: bool | None = None
        self._busy_at = 0.0
        self._idle_at = 0.0
        self._refs_key: tuple[int, int] | None = None
        self._refs: tuple[int, bool] = (0, False)
        self._snap: dict = {}
        try:
            self.sample()
        except Exception:
            # Same reason run() swallows: this runs at import, before the socket is even
            # bound, so anything that escapes here means the server never starts at all.
            # An empty first snapshot degrades the Ribbon to today's behaviour, which the
            # client's failure register already handles; a dead process does not.
            pass

    def _update_busy(self, now: float) -> None:
        jiffies = _agent_jiffies()
        if jiffies is None:
            # No agent process, or /proc unreadable. Report null and never guess — a
            # fabricated "still working" on a dead agent is exactly the failure this
            # product exists to avoid.
            self._busy = None
            self._prev_jiffies = None
            self._over = self._under = 0
            return
        if self._prev_jiffies is not None:
            # Negative when the set of agent pids changed under us; that is not work.
            delta = max(0, jiffies - self._prev_jiffies)
            if delta / max(PULSE_INTERVAL, 1e-6) >= BUSY_JIFFIES_PER_S:
                self._over, self._under = self._over + 1, 0
            else:
                self._under, self._over = self._under + 1, 0
            # Latch on two consecutive samples in each direction, so a single keystroke
            # repaint cannot read as "thinking" and one quiet tick mid-write cannot read
            # as "finished".
            if self._over >= 2 and self._busy is not True:
                self._busy, self._busy_at = True, now
            elif self._under >= 2 and self._busy is not False:
                self._busy, self._idle_at = False, now
        self._prev_jiffies = jiffies

    def _index_facts(self, index: Path) -> tuple[int, bool]:
        """(offline_refs, keeps_running), recomputed only when index.html actually changes.

        The naive version read and regexed the whole file every single window. AGENTS.md
        rule 1 tells the agent to inline every image, so index.html is routinely megabytes
        of base64 — measured at 5% of a core, permanently, on a 4 MB file with the agent
        sitting idle, and this pod shares its CPU with the agent. Keyed on (mtime_ns,
        size), which is the same signal the fingerprint uses, so a changed file is never
        served a stale count.
        """
        try:
            st = index.stat()
        except OSError:
            self._refs_key, self._refs = None, (0, False)
            return 0, False
        key = (st.st_mtime_ns, st.st_size)
        if key != self._refs_key:
            self._refs_key, self._refs = key, _scan_index(index)
        return self._refs

    def sample(self) -> None:
        now = time.time()
        project = active_project()
        root = PROJECTS_ROOT / project
        entries, truncated = _scan(root)
        fp = _fingerprint(entries)

        # The first sample of a process establishes the baseline; it is not a change.
        # Without this every browser opening the page would see rev tick once for nothing.
        if self._fp is not None and fp != self._fp:
            self._rev += 1
        self._fp, self._project = fp, project

        newest = max((m for _, m, _ in entries), default=None)
        if newest is None:
            last_change_ms, changed = NEVER_MS, []
        else:
            last_change_ms = max(0, int((now - newest / 1e9) * 1000))
            fresh = sorted(entries, key=lambda e: e[1], reverse=True)
            cutoff = now - CHANGED_WINDOW_MS / 1000
            changed = [rel for rel, m, _ in fresh if m / 1e9 >= cutoff][:3]

        self._update_busy(now)
        refs, keeps_running = self._index_facts(root / "index.html")
        snap = {
            "project": project,
            "rev": self._rev,
            "fp": fp,
            "changed": changed,
            "last_change_ms": last_change_ms,
            "has_index": (root / "index.html").is_file(),
            "offline_refs": refs,
            # Whether the page keeps running once loaded. Drives which of the two run
            # buttons is the primary one; see LIVE_PAGE.
            "keeps_running": keeps_running,
            "busy": self._busy,
            "busy_ms": int((now - self._busy_at) * 1000) if self._busy is True else 0,
            "idle_ms": int((now - self._idle_at) * 1000) if self._busy is False else 0,
            "published": _has_published(project),
            "published_fp": read_published_fp(project),
            "truncated": truncated,
        }
        with self._lock:
            self._snap = snap

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._snap)

    def run(self) -> None:
        while True:
            time.sleep(PULSE_INTERVAL)
            try:
                self.sample()
            except Exception:
                # This thread must outlive every possible filesystem race — a deleted
                # project, a file swapped mid-stat, a permission change. If it dies the UI
                # goes silent forever, which is worse than any single bad sample.
                pass


def read_published_fp(project: str) -> str:
    try:
        return json.loads((META_ROOT / f"{project}.json").read_text()).get("published_fp", "")
    except (OSError, json.JSONDecodeError, AttributeError):
        return ""


def write_published_fp(project: str) -> None:
    try:
        META_ROOT.mkdir(parents=True, exist_ok=True)
        entries, _ = _scan(PROJECTS_ROOT / project)
        (META_ROOT / f"{project}.json").write_text(
            json.dumps({"published_fp": _fingerprint(entries)}) + "\n")
    except OSError:
        # Losing this only costs the "Update the link" hint. It must never fail a publish
        # that actually succeeded.
        pass


PULSE = Pulse()


class Handler(BaseHTTPRequestHandler):
    server_version = "workspace-shell"

    def log_message(self, fmt, *args):  # quieter than the default one-line-per-asset
        pass

    # ---------------------------------------------------------------- helpers

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # The preview is the child's own app mid-edit; a cached copy makes it look like
        # their change did nothing.
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, payload: dict):
        self._send(code, json.dumps(payload).encode(), "application/json")

    def _serve_file(self, path: Path, fallback_ctype="application/octet-stream", extra=None):
        if not path.is_file():
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        ctypes = {
            ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
            ".js": "text/javascript; charset=utf-8", ".mjs": "text/javascript; charset=utf-8",
            ".json": "application/json",
            # A WebAssembly engine build (Godot, Unity, a hand-rolled emscripten runtime)
            # is only reached through the preview, and `WebAssembly.instantiateStreaming`
            # REFUSES a response that is not exactly `application/wasm` — get this wrong
            # and a 3D game compiles to a blank canvas with a console error the child
            # never sees. The default file server had no idea what a .wasm was, so this is
            # the load-bearing line for every engine export, not a nicety.
            ".wasm": "application/wasm",
            # Godot packs the whole game into these opaque side-blobs; Unity uses .data.
            # Served as text/* the browser tries to sniff them and the loader aborts.
            ".pck": "application/octet-stream", ".data": "application/octet-stream",
            ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".gif": "image/gif", ".svg": "image/svg+xml", ".ico": "image/x-icon",
            ".webp": "image/webp", ".mp3": "audio/mpeg", ".wav": "audio/wav",
            ".ogg": "audio/ogg", ".m4a": "audio/mp4", ".mp4": "video/mp4",
            ".ttf": "font/ttf", ".otf": "font/otf", ".woff": "font/woff",
            ".woff2": "font/woff2", ".glb": "model/gltf-binary", ".gltf": "model/gltf+json",
            ".txt": "text/plain; charset=utf-8", ".map": "application/json",
        }
        self._send(200, path.read_bytes(),
                   ctypes.get(path.suffix.lower(), fallback_ctype), extra)

    def _safe_join(self, root: Path, rel: str) -> Path | None:
        """Resolve rel under root, refusing anything that escapes it.

        The preview serves whatever the agent wrote, so a path like ../../etc/passwd must
        not resolve. Compared after resolution rather than by string inspection.
        """
        target = (root / rel.lstrip("/")).resolve()
        try:
            target.relative_to(root.resolve())
        except ValueError:
            return None
        return target

    # ---------------------------------------------------------------- routes

    def _authorised(self) -> bool:
        """Every request must carry the pod's own token.

        Compared with compare_digest: the check runs on every asset the browser fetches,
        which is exactly the shape that makes a naive == comparison measurable.
        """
        if not INTERNAL_TOKEN:
            return False
        sent = self.headers.get("X-Workspace-Token", "")
        return hmac.compare_digest(sent, INTERNAL_TOKEN)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path

        if not self._authorised():
            self._send(403, b"denied", "text/plain; charset=utf-8")
            return

        if route in ("/", "/index.html"):
            self._serve_file(STATIC / "index.html")
        elif route.startswith("/static/"):
            f = self._safe_join(STATIC, route[len("/static/"):])
            self._serve_file(f) if f else self._send(403, b"denied", "text/plain")
        elif route == "/api/state":
            active = active_project()
            self._json(200, {
                "user": USER,
                "project": active,
                "projects": list_projects(),
                "has_index": (project_dir() / "index.html").is_file(),
                # Via the guarded helper, not a bare iterdir(). An unreadable /published
                # took the sampler down for exactly this reason; the same volume, the same
                # OSError, would otherwise take /api/state down with it — and /api/state is
                # what the UI falls back to when everything else fails.
                "published": _has_published(active),
                # Each project publishes to its own path, so making a second thing no
                # longer overwrites the first one a parent was sent.
                "published_url": f"{PUBLISH_URL}/live/{USER}/{active}/" if PUBLISH_URL else "",
                "model": _agent_model(),
                "models": MODELS,
            })
        elif route == "/api/pulse":
            # The handler does no work: the sampler already computed this. Everything here
            # is additive, so a pulse failure can never break project switching.
            self._json(200, PULSE.snapshot())
        elif route.startswith("/preview"):
            rel = route[len("/preview"):] or "/"
            # Percent-decode before doing anything else. A browser escapes any character
            # it must, so "my sprite.png" arrives as "my%20sprite.png" and, undecoded,
            # named a file that does not exist — the image was broken in Look and perfect
            # in the published copy, which a child has no way to make sense of. Decoding
            # first also means the refusals below see the real name: ".%67it" is .git.
            # Traversal stays safe because _safe_join resolves and then checks containment
            # rather than inspecting the string.
            rel = urllib.parse.unquote(rel)
            if rel.endswith("/"):
                rel += "index.html"
            # The preview is a sandboxed opaque origin that can still fetch same-server
            # paths. It has no business reading the repo internals or our publish
            # bookkeeping, so those are refused by name before the path is even resolved.
            segments = [s for s in rel.split("/") if s]
            if segments and segments[0] in (".git", ".meta"):
                self._send(404, b"not found", "text/plain; charset=utf-8")
                return
            f = self._safe_join(project_dir(), rel)
            if f is None:
                self._send(403, b"denied", "text/plain; charset=utf-8")
            elif not f.is_file():
                self._send(404, b"<!doctype html><meta charset=utf-8>"
                                b"<body style='font:16px system-ui;padding:2rem;color:#555'>"
                                b"Nothing to preview yet. Ask the agent to make an "
                                b"<code>index.html</code>.</body>", "text/html; charset=utf-8")
            else:
                # Cross-Origin-Resource-Policy lets these assets load into an embedder
                # that has turned on cross-origin isolation (COEP: require-corp) — which a
                # THREADED WebAssembly build needs. The single-threaded engine export that
                # is the default here does not require it, so this is belt-and-braces, not
                # load-bearing; it costs one header and means a threaded export does not
                # silently fail to fetch its own .wasm the day someone tries one.
                self._serve_file(f, extra={"Cross-Origin-Resource-Policy": "cross-origin"})
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            body = {}

        if not self._authorised():
            self._send(403, b"denied", "text/plain; charset=utf-8")
            return

        if parsed.path == "/api/projects":
            raw = (body.get("name") or "").strip()
            if UNSAFE.search(raw):
                self._json(400, {"ok": False, "message":
                    "That name has characters I cannot use. Try letters, numbers and spaces."})
                return
            name = slugify(raw)
            if not SLUG_OK.match(name):
                self._json(400, {"ok": False, "message":
                    "Use letters, numbers and dashes — like 'unicorn-game'."})
                return
            d = PROJECTS_ROOT / name
            if d.exists():
                self._json(400, {"ok": False, "message": f"'{name}' already exists."})
                return
            d.mkdir(parents=True)
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=str(d), timeout=30)
            ACTIVE_FILE.write_text(name + "\n")
            self._json(200, {"ok": True, "project": name,
                             "message": f"Made '{name}' and switched to it."})

        elif parsed.path == "/api/switch":
            name = (body.get("name") or "").strip()
            if not SLUG_OK.match(name) or not (PROJECTS_ROOT / name).is_dir():
                self._json(400, {"ok": False, "message": "No such project."})
                return
            ACTIVE_FILE.write_text(name + "\n")
            self._json(200, {"ok": True, "project": name, "message": f"Switched to '{name}'."})

        elif parsed.path == "/api/reset":
            # Empty the project but keep it and keep its git history, so a reset is
            # recoverable. Deleting outright is a separate, explicit action.
            d = project_dir()
            for child in d.iterdir():
                if child.name == ".git":
                    continue
                shutil.rmtree(child) if child.is_dir() else child.unlink()
            self._json(200, {"ok": True, "message":
                "Cleared. Your old versions are still in git history."})

        elif parsed.path == "/api/delete":
            name = (body.get("name") or "").strip()
            if not SLUG_OK.match(name) or not (PROJECTS_ROOT / name).is_dir():
                self._json(400, {"ok": False, "message": "No such project."})
                return
            if len(list_projects()) <= 1:
                self._json(400, {"ok": False, "message":
                    "That is your only project — make another one first."})
                return
            shutil.rmtree(PROJECTS_ROOT / name)
            shutil.rmtree(PUBLISHED_ROOT / name, ignore_errors=True)
            # Otherwise a project made again under the same name inherits the old
            # fingerprint and claims to be published when nothing is there.
            (META_ROOT / f"{name}.json").unlink(missing_ok=True)
            # Repair .active by reading the FILE, not by asking active_project(): the
            # directory is already gone, so active_project() has fallen back and can never
            # equal `name` again. Comparing against its answer meant .active was left
            # naming a deleted project for the rest of the session — and the terminal,
            # which reads .active directly and mkdir -p's it, then quietly resurrected the
            # thing that was just deleted.
            try:
                pointer = ACTIVE_FILE.read_text().strip()
            except OSError:
                pointer = ""
            if not pointer or not (PROJECTS_ROOT / pointer).is_dir():
                ACTIVE_FILE.write_text(list_projects()[0]["name"] + "\n")
            self._json(200, {"ok": True, "message": f"Deleted '{name}'.",
                             "project": active_project()})

        elif parsed.path == "/api/publish":
            proc = subprocess.run(["/usr/local/bin/publish"], capture_output=True,
                                  text=True, timeout=120, cwd=str(project_dir()))
            ok = proc.returncode == 0
            if ok:
                # Remember what was published so the share button can tell "Live" from
                # "Update the link" without re-reading the published copy.
                write_published_fp(active_project())
            self._json(200 if ok else 400, {
                "ok": ok,
                # The command's own words. It already explains the index.html requirement
                # and the size limit better than a second copy of that logic would.
                "message": (proc.stdout or proc.stderr).strip()[-600:],
                "url": f"{PUBLISH_URL}/live/{USER}/{active_project()}/" if ok and PUBLISH_URL else "",
            })
        elif parsed.path == "/api/session/new":
            # Ask for a clean agent on the NEXT terminal connection. It cannot be done to
            # a running one: ttyd spawns the shell per websocket, so the switch happens
            # when the client reloads that frame — which is exactly what the UI does next.
            META_ROOT.mkdir(parents=True, exist_ok=True)
            (META_ROOT / f"{active_project()}.new-session").write_text("1\n")
            self._json(200, {"ok": True, "message": "Starting a fresh chat with the agent."})

        elif parsed.path == "/api/model":
            try:
                _set_agent_model(body.get("model", ""))
            except (ValueError, OSError) as exc:
                self._json(400, {"ok": False, "message": str(exc)})
                return
            # Same flag "New chat" uses: the next terminal connection starts clean, which
            # is the only way the new model actually applies.
            META_ROOT.mkdir(parents=True, exist_ok=True)
            (META_ROOT / f"{active_project()}.new-session").write_text("1\n")
            self._json(200, {"ok": True, "model": _agent_model(),
                             "message": "Switched. Starting a fresh chat on the new model."})
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")


if __name__ == "__main__":
    PROJECTS_ROOT.mkdir(parents=True, exist_ok=True)
    project_dir().mkdir(parents=True, exist_ok=True)
    # Daemon so a shutdown is never held open by the sampler. It re-reads the active
    # project every tick, which is what moves it to a new project within one window of a
    # switch — no signalling, nothing to get out of step.
    try:
        PULSE.sample()
    except Exception:
        pass  # As in __init__: a bad sample must never stop the server binding its port.
    threading.Thread(target=PULSE.run, daemon=True).start()
    if not INTERNAL_TOKEN:
        raise SystemExit(
            "refusing to start: WS_INTERNAL_TOKEN is not set. It is the credential the "
            "control plane presents; without it this would be an unauthenticated shell "
            "API on the pod network."
        )
    # Bound to every interface, guarded by the token above and by a NetworkPolicy that
    # admits only the control-plane pod. entrypoint.sh explains why loopback stopped
    # being possible and why that trade was made deliberately.
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
