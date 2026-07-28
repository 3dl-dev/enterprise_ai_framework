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
  /api/publish    run `publish`, return the parent-facing link
  /api/model      read/write the agent's model
  /api/projects   create a project and switch to it
  /api/switch     change the active project
  /api/reset      empty the active project, keeping its git history
  /api/delete     remove a project and anything it published
  /terminal/      NOT handled here — oauth2-proxy routes it straight to ttyd
"""

import json
import os
import re
import shutil
import subprocess
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJECTS_ROOT = Path(os.environ.get("WS_PROJECTS_ROOT", "/workspace/projects"))
ACTIVE_FILE = PROJECTS_ROOT / ".active"
USER = os.environ.get("WS_USER", "coder")
PUBLISH_URL = os.environ.get("WS_PUBLISH_URL", "")
PUBLISHED_ROOT = Path("/published") / USER

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
    try:
        name = ACTIVE_FILE.read_text().strip()
        if name and (PROJECTS_ROOT / name).is_dir():
            return name
    except OSError:
        pass
    existing = sorted(p.name for p in PROJECTS_ROOT.glob("*") if p.is_dir())
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

# Offered in Settings. Kept in step with the gateway catalogue; the agent config declares
# the same two with their real context and output limits.
MODELS = [
    {"id": "glm-5.2@deepinfra", "label": "GLM 5.2 — better at hard things"},
    {"id": "glm-4.7@deepinfra", "label": "GLM 4.7 — faster and cheaper"},
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
    """Write the model into the project config the agent reads on next start.

    Deliberately does not restart or signal a running agent: yanking the model out from
    under an in-flight conversation is worse than the change taking effect next time.
    """
    if model_id not in {m["id"] for m in MODELS}:
        raise ValueError(f"unknown model: {model_id}")
    cfg = project_dir() / "opencode.json"
    data = json.loads(cfg.read_text()) if cfg.exists() else {}
    data["model"] = f"enterprise-ai/{model_id}"
    cfg.write_text(json.dumps(data, indent=2) + "\n")


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

    def _serve_file(self, path: Path, fallback_ctype="text/plain; charset=utf-8"):
        if not path.is_file():
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        ctypes = {
            ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
            ".js": "text/javascript; charset=utf-8", ".json": "application/json",
            ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".gif": "image/gif", ".svg": "image/svg+xml", ".ico": "image/x-icon",
            ".webp": "image/webp", ".mp3": "audio/mpeg", ".wav": "audio/wav",
        }
        self._send(200, path.read_bytes(), ctypes.get(path.suffix.lower(), fallback_ctype))

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

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path

        if route in ("/", "/index.html"):
            self._serve_file(STATIC / "index.html")
        elif route.startswith("/static/"):
            f = self._safe_join(STATIC, route[len("/static/"):])
            self._serve_file(f) if f else self._send(403, b"denied", "text/plain")
        elif route == "/api/state":
            active = active_project()
            pub = PUBLISHED_ROOT / active
            self._json(200, {
                "user": USER,
                "project": active,
                "projects": list_projects(),
                "has_index": (project_dir() / "index.html").is_file(),
                "published": pub.is_dir() and any(pub.iterdir()),
                # Each project publishes to its own path, so making a second thing no
                # longer overwrites the first one a parent was sent.
                "published_url": f"{PUBLISH_URL}/live/{USER}/{active}/" if PUBLISH_URL else "",
                "model": _agent_model(),
                "models": MODELS,
            })
        elif route.startswith("/preview"):
            rel = route[len("/preview"):] or "/"
            if rel.endswith("/"):
                rel += "index.html"
            f = self._safe_join(project_dir(), rel)
            if f is None:
                self._send(403, b"denied", "text/plain; charset=utf-8")
            elif not f.is_file():
                self._send(404, b"<!doctype html><meta charset=utf-8>"
                                b"<body style='font:16px system-ui;padding:2rem;color:#555'>"
                                b"Nothing to preview yet. Ask the agent to make an "
                                b"<code>index.html</code>.</body>", "text/html; charset=utf-8")
            else:
                self._serve_file(f)
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
            if active_project() == name or not ACTIVE_FILE.exists():
                ACTIVE_FILE.write_text(list_projects()[0]["name"] + "\n")
            self._json(200, {"ok": True, "message": f"Deleted '{name}'.",
                             "project": active_project()})

        elif parsed.path == "/api/publish":
            proc = subprocess.run(["/usr/local/bin/publish"], capture_output=True,
                                  text=True, timeout=120, cwd=str(project_dir()))
            ok = proc.returncode == 0
            self._json(200 if ok else 400, {
                "ok": ok,
                # The command's own words. It already explains the index.html requirement
                # and the size limit better than a second copy of that logic would.
                "message": (proc.stdout or proc.stderr).strip()[-600:],
                "url": f"{PUBLISH_URL}/live/{USER}/{active_project()}/" if ok and PUBLISH_URL else "",
            })
        elif parsed.path == "/api/model":
            try:
                _set_agent_model(body.get("model", ""))
            except (ValueError, OSError) as exc:
                self._json(400, {"ok": False, "message": str(exc)})
                return
            self._json(200, {"ok": True, "model": _agent_model(),
                             "message": "Saved. It applies the next time the agent starts."})
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")


if __name__ == "__main__":
    PROJECTS_ROOT.mkdir(parents=True, exist_ok=True)
    project_dir().mkdir(parents=True, exist_ok=True)
    # Loopback only. oauth2-proxy shares this pod's network namespace and is the only
    # thing that can reach it; binding wider would put an unauthenticated API on the node.
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
