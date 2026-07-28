"""Hermetic tests for the workspace shell server.

No cluster, no docker, no network, no bundle. Each test launches shell-server.py as a
subprocess against a throwaway WS_PROJECTS_ROOT on an ephemeral loopback port. That is
deliberately the real program rather than an import of its helpers: the things most
likely to break at camp are the wiring — a route that 500s, a sampler thread that dies on
a deleted directory — and none of those are visible when you unit-test the functions.

The sampler window is driven down to 100 ms via WS_PULSE_INTERVAL so the suite can watch
revisions move without sleeping through 1 Hz production windows. Every wait in here polls
for the condition it wants; there are no fixed sleeps longer than one sampler window.
"""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

SERVER = Path(__file__).resolve().parent.parent / "deploy" / "workspace" / "shell-server.py"

PULSE_INTERVAL = 0.1
TIMEOUT = 5.0

# The exact key set /api/state has today. §9 of the binding spec freezes this response
# byte for byte so that everything added for the Ribbon is additive — a broken pulse must
# never be able to take project switching down with it. If this assertion fails, someone
# widened /api/state and the guarantee is gone.
STATE_KEYS = {
    "user", "project", "projects", "has_index", "published",
    "published_url", "model", "models",
}

PULSE_TYPES = {
    "project": str, "rev": int, "fp": str, "changed": list,
    "last_change_ms": int, "has_index": bool, "offline_refs": int,
    # Whether the page keeps running after load. Decides which run button the workshop
    # emphasises, so a wrong TYPE here silently changes what a child is nudged towards.
    "keeps_running": bool,
    "busy_ms": int, "idle_ms": int, "published": bool,
    "published_fp": str, "truncated": bool,
}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _foreign_agents(exclude: set[int] = frozenset()) -> list[str]:
    """Mirror of the server's own pid scan: agent processes this suite did not start.

    `busy` is a property of the WHOLE HOST, because the server scans every pid — in the
    pod that is correct and exact, since the pod has its own PID namespace and the only
    agent in it is the child's. On a developer box or a shared CI runner it is not: any
    other opencode on the machine — a second terminal, another agent in the same
    workspace — is indistinguishable from the one under test.

    Observed, not hypothetical: a concurrent `opencode run` elsewhere on this machine made
    `test_busy_is_null...` skip and `test_busy_latches...` fail outright. Naming the
    offending processes in the message is the point — the difference between "the host is
    dirty" and "the pulse is broken" must never have to be guessed at.
    """
    found = []
    uid = os.getuid()
    for pid in os.listdir("/proc"):
        if not pid.isdigit() or int(pid) in exclude:
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                argv = [a for a in fh.read().split(b"\0") if a]
            if not argv:
                continue
            names = [os.path.basename(argv[0].decode("utf-8", "replace"))]
            # Mirrors the server: aider is a console script, so it shows up as its
            # interpreter with the real name in argv[1].
            if names[0].startswith("python") and len(argv) > 1:
                names.append(os.path.basename(argv[1].decode("utf-8", "replace")))
            if set(names) & {"opencode", "aider"} and os.stat(f"/proc/{pid}").st_uid == uid:
                found.append(f"{pid}: {' '.join(a.decode('utf-8', 'replace') for a in argv[:4])}")
        except OSError:
            continue
    return found


# The credential the control plane presents. The shell server refuses to start without
# one and refuses every request that does not carry it — see entrypoint.sh for why the
# pod stopped being loopback-only when the workshop became a tab in the portal.
TEST_TOKEN = "test-workspace-token"


class Shell:
    """A running shell-server plus the temp root it serves."""

    def __init__(self, root: Path, port: int, proc: subprocess.Popen):
        self.root, self.port, self.proc = root, port, proc
        self.base = f"http://127.0.0.1:{port}"
        self.headers = {"X-Workspace-Token": TEST_TOKEN}

    def get(self, path: str) -> httpx.Response:
        return httpx.get(self.base + path, timeout=TIMEOUT, headers=self.headers)

    def post(self, path: str, payload: dict) -> httpx.Response:
        return httpx.post(self.base + path, json=payload, timeout=TIMEOUT,
                          headers=self.headers)

    def pulse(self) -> dict:
        r = self.get("/api/pulse")
        assert r.status_code == 200, r.text
        return r.json()

    def raw_get(self, path: str) -> tuple[int, bytes]:
        """Send a request line verbatim, bypassing client-side URL normalisation.

        httpx resolves "../" before it ever reaches the wire, which would make a traversal
        test silently prove nothing. The attack we care about arrives as raw bytes, so the
        test has to send raw bytes.
        """
        with socket.create_connection(("127.0.0.1", self.port), timeout=TIMEOUT) as s:
            s.sendall(
                # The token goes on the raw request too. Without it every traversal case
                # below would be answered 403 by the auth check and would prove only that
                # the auth check works — never that the path handling is safe.
                (f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                 f"X-Workspace-Token: {TEST_TOKEN}\r\nConnection: close\r\n\r\n").encode()
            )
            buf = b""
            while chunk := s.recv(65536):
                buf += chunk
        head, _, body = buf.partition(b"\r\n\r\n")
        return int(head.split(b" ", 2)[1]), body

    def wait_for(self, predicate, what: str, timeout: float = TIMEOUT):
        """Poll a pulse predicate. Returns the snapshot that satisfied it."""
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            last = self.pulse()
            if predicate(last):
                return last
            time.sleep(PULSE_INTERVAL / 2)
        pytest.fail(f"timed out waiting for {what}; last pulse was {last}")

    def settle(self, windows: int = 3) -> dict:
        """Let the sampler run N full windows, then return the snapshot.

        Used to prove a NON-event — that something did not bump the revision. A single
        window would not distinguish "never bumps" from "has not bumped yet".
        """
        time.sleep(PULSE_INTERVAL * windows)
        return self.pulse()


@pytest.fixture
def shell(tmp_path: Path):
    root = tmp_path / "projects"
    (root / "alpha").mkdir(parents=True)
    (root / ".active").write_text("alpha\n")

    port = _free_port()
    env = {
        **os.environ,
        "WS_PROJECTS_ROOT": str(root),
        "WS_SHELL_PORT": str(port),
        "WS_PULSE_INTERVAL": str(PULSE_INTERVAL),
        "WS_USER": "tester",
        "WS_PUBLISH_URL": "http://example.invalid",
        "WS_INTERNAL_TOKEN": TEST_TOKEN,
    }
    proc = subprocess.Popen(
        [sys.executable, str(SERVER)], env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    sh = Shell(root, port, proc)

    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            pytest.fail(f"server exited early: {proc.communicate()[1].decode()}")
        try:
            sh.get("/api/state")
            break
        except httpx.HTTPError:
            time.sleep(0.05)
    else:
        proc.kill()
        pytest.fail("server never came up")

    yield sh

    proc.kill()
    proc.wait(timeout=TIMEOUT)


# ------------------------------------------------------------------ /api/state

def test_state_shape_is_unchanged(shell):
    """The golden-key assertion. /api/state is frozen; the pulse work must be additive."""
    body = shell.get("/api/state").json()
    assert set(body) == STATE_KEYS
    assert body["project"] == "alpha"
    assert body["user"] == "tester"
    assert isinstance(body["projects"], list)
    assert body["has_index"] is False


# ------------------------------------------------------------------ /api/pulse

def test_pulse_reports_full_shape_with_correct_types(shell):
    p = shell.pulse()
    assert set(p) == set(PULSE_TYPES) | {"busy"}
    for key, want in PULSE_TYPES.items():
        assert isinstance(p[key], want), f"{key} was {type(p[key])}, wanted {want}"
    # Tri-state: true, false, or "cannot observe". Never anything else.
    assert p["busy"] in (True, False, None)
    assert p["project"] == "alpha"
    assert shell.get("/api/pulse").headers["Cache-Control"] == "no-store"


def test_pulse_is_stable_with_no_filesystem_change(shell):
    first = shell.pulse()
    second = shell.settle()
    assert (first["fp"], first["rev"]) == (second["fp"], second["rev"])


def test_writing_a_file_bumps_revision_by_exactly_one(shell):
    before = shell.pulse()["rev"]
    (shell.root / "alpha" / "index.html").write_text("<h1>hi</h1>")
    after = shell.wait_for(lambda p: p["rev"] != before, "revision to move")
    assert after["rev"] == before + 1
    assert after["changed"][0] == "index.html"
    assert after["last_change_ms"] < 4000
    # And it stays put: a rescan that finds nothing new is not a change.
    assert shell.settle()["rev"] == before + 1


def test_git_and_node_modules_churn_does_not_bump_revision(shell):
    (shell.root / "alpha" / "index.html").write_text("<h1>hi</h1>")
    settled = shell.wait_for(lambda p: p["has_index"], "index.html to register")
    rev = settled["rev"]

    for noise in ("alpha/.git/objects", "alpha/node_modules/left-pad", "alpha/__pycache__"):
        d = shell.root / noise
        d.mkdir(parents=True, exist_ok=True)
        (d / "junk").write_text("churn")
    assert shell.settle(4)["rev"] == rev

    # A second write into the same excluded trees must also stay invisible.
    (shell.root / "alpha" / ".git" / "objects" / "junk").write_text("more churn")
    assert shell.settle(4)["rev"] == rev


def test_has_index_flips_with_the_file(shell):
    assert shell.pulse()["has_index"] is False
    index = shell.root / "alpha" / "index.html"
    index.write_text("<h1>hi</h1>")
    shell.wait_for(lambda p: p["has_index"] is True, "has_index true")
    index.unlink()
    shell.wait_for(lambda p: p["has_index"] is False, "has_index false")


def test_offline_refs_counts_only_loadable_references(shell):
    """The highest-value line in the server. The pod has no egress; a CDN <script src>
    renders a blank page with no explanation, and this is what turns that into a sentence."""
    (shell.root / "alpha" / "index.html").write_text(
        '<script src="https://cdn.example.com/x.js"></script>\n'
        '<link rel="stylesheet" href="//fonts.example.com/f.css">\n'
        '<IMG SRC="http://example.com/a.png">\n'
        '<a href="https://example.com">a link is not a load</a>\n'
        '<script src="game.js"></script>\n'
        '<img src="cat.png">\n'
    )
    p = shell.wait_for(lambda p: p["has_index"], "index.html to register")
    assert p["offline_refs"] == 3


def test_offline_refs_is_zero_for_a_self_contained_page(shell):
    (shell.root / "alpha" / "index.html").write_text(
        "<style>body{color:red}</style><script>const x=1//2\n</script>"
    )
    p = shell.wait_for(lambda p: p["has_index"], "index.html to register")
    assert p["offline_refs"] == 0


def test_busy_is_null_when_no_agent_is_running(shell):
    p = shell.pulse()
    foreign = _foreign_agents()
    if foreign:
        pytest.skip("the host is not clean; these agent processes are not ours:\n  "
                    + "\n  ".join(foreign))
    assert p["busy"] is None
    assert p["busy_ms"] == 0 and p["idle_ms"] == 0


def test_busy_latches_through_all_three_states(shell, tmp_path):
    """Drive the CPU sampler with a real process actually named `opencode`.

    This is the one part of the pulse that cannot be proven by writing files, and it is
    what the whole Ribbon rests on. The fake agent burns CPU for two seconds and then
    sleeps, so a single process exercises working -> finished; killing it then exercises
    "cannot observe", which is a different answer from "not working" and must not be
    conflated with it.
    """
    fake = tmp_path / "opencode"
    fake.write_bytes(Path(sys.executable).read_bytes())
    fake.chmod(0o755)

    proc = subprocess.Popen(
        [str(fake), "-c",
         "import time\n"
         "end = time.time() + 2.0\n"
         "while time.time() < end: pass\n"
         "time.sleep(60)\n"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        busy = shell.wait_for(lambda p: p["busy"] is True, "the agent to read as working")
        assert busy["busy_ms"] >= 0 and busy["idle_ms"] == 0

        idle = shell.wait_for(lambda p: p["busy"] is False,
                              "the agent to read as finished", timeout=8.0)
        assert idle["idle_ms"] >= 0 and idle["busy_ms"] == 0
    finally:
        proc.kill()
        proc.wait(timeout=TIMEOUT)

    # No process at all is "cannot observe", not "idle". The Ribbon must never invent
    # activity it cannot see, and must not claim the agent finished when it has vanished.
    #
    # Only assertable on a clean host: the server scans every pid, so any other opencode
    # on this machine keeps the answer at true/false and this reads as a product failure
    # when it is a dirty box. Left as an explicit skip WITH the offending command lines
    # rather than a bare timeout — this exact case was hit while verifying, and the
    # timeout message gave no hint that the cause was another process entirely.
    foreign = _foreign_agents()
    if foreign:
        pytest.skip("cannot assert 'no agent visible'; these are not ours:\n  "
                    + "\n  ".join(foreign))
    shell.wait_for(lambda p: p["busy"] is None, "busy to go unobservable")


def test_last_change_ms_never_claims_a_write_that_never_happened(shell):
    """An empty project must not report a recent change.

    Guards a specific client-side trap: the Ribbon tests `last_change_ms < 4000`, and in
    JavaScript `null < 4000` is true. A null here would narrate a write into a project
    that has never had a file in it.
    """
    p = shell.pulse()
    assert p["changed"] == []
    assert p["last_change_ms"] >= 4000


# ------------------------------------------------------------------ project switching

def test_project_switch_moves_the_watcher(shell):
    (shell.root / "beta").mkdir()
    (shell.root / "beta" / "index.html").write_text("<h1>beta</h1>")

    r = shell.post("/api/switch", {"name": "beta"})
    assert r.status_code == 200 and r.json()["ok"] is True
    p = shell.wait_for(lambda p: p["project"] == "beta", "watcher to follow the switch")
    assert p["has_index"] is True

    # And it now tracks changes in the NEW project, not the old one.
    rev = p["rev"]
    (shell.root / "beta" / "second.html").write_text("more")
    after = shell.wait_for(lambda p: p["rev"] != rev, "revision in the new project")
    assert after["changed"][0] == "second.html"


def test_created_project_becomes_active_and_is_watched(shell):
    r = shell.post("/api/projects", {"name": "Space Cats!!"})
    assert r.status_code == 200, r.text
    assert r.json()["project"] == "space-cats"
    shell.wait_for(lambda p: p["project"] == "space-cats", "watcher to follow creation")


@pytest.mark.parametrize("name", ["../escape", "a/b", "a\\b", "..", "x..y", "foo/../bar"])
def test_path_ish_project_names_are_refused_not_rewritten(shell, name):
    """A rejected name is easy to explain; a silently rewritten one is not.

    "../escape" must never quietly become "escape" — that is a traversal attempt answered
    with a working project.
    """
    r = shell.post("/api/projects", {"name": name})
    assert r.status_code == 400
    assert r.json()["ok"] is False
    assert not (shell.root / "escape").exists()
    assert not (shell.root / "bar").exists()
    assert {p.name for p in shell.root.iterdir() if p.is_dir()} == {"alpha"}


# ------------------------------------------------------------------ /preview hardening

def test_preview_serves_the_project(shell):
    (shell.root / "alpha" / "index.html").write_text("<h1>mine</h1>")
    r = shell.get("/preview/")
    assert r.status_code == 200
    assert b"<h1>mine</h1>" in r.content


def test_preview_without_an_index_explains_itself(shell):
    r = shell.get("/preview/")
    assert r.status_code == 404
    assert b"index.html" in r.content


@pytest.mark.parametrize("path", ["/preview/.git/config", "/preview/.meta/alpha.json"])
def test_preview_refuses_repo_internals(shell, path):
    (shell.root / "alpha" / ".git").mkdir(exist_ok=True)
    (shell.root / "alpha" / ".git" / "config").write_text("[core]\n")
    status, _ = shell.raw_get(path)
    assert status == 404


@pytest.mark.parametrize("path", [
    "/preview/../../etc/passwd",
    "/preview/../.active",
    "/static/../shell-server.py",
    "/static/../../workspace/publish",
])
def test_traversal_is_refused(shell, path):
    status, body = shell.raw_get(path)
    assert status == 403, f"{path} returned {status}: {body[:200]!r}"


# ------------------------------------------------------------------ resilience

def test_watcher_survives_the_project_directory_vanishing(shell):
    (shell.root / "alpha" / "index.html").write_text("<h1>hi</h1>")
    shell.wait_for(lambda p: p["has_index"], "index.html to register")

    # This is what /api/reset and an impatient `rm -rf` both look like from in here.
    import shutil
    shutil.rmtree(shell.root / "alpha")

    p = shell.settle(4)
    assert p["has_index"] is False
    assert shell.proc.poll() is None, "server died when the project vanished"

    # The sampler is still alive, not merely the HTTP thread: it must pick up new work.
    (shell.root / "alpha").mkdir()
    (shell.root / "alpha" / "index.html").write_text("<h1>back</h1>")
    shell.wait_for(lambda p: p["has_index"] is True, "sampler to recover")


def test_reset_empties_the_project_without_killing_the_sampler(shell):
    (shell.root / "alpha" / "index.html").write_text("<h1>hi</h1>")
    (shell.root / "alpha" / ".git").mkdir(exist_ok=True)
    (shell.root / "alpha" / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    shell.wait_for(lambda p: p["has_index"], "index.html to register")

    r = shell.post("/api/reset", {})
    assert r.status_code == 200 and r.json()["ok"] is True
    shell.wait_for(lambda p: p["has_index"] is False, "reset to clear the index")
    # git history survives a reset; that is the whole difference from delete.
    assert (shell.root / "alpha" / ".git" / "HEAD").is_file()


def test_unknown_routes_still_404(shell):
    assert shell.get("/api/nope").status_code == 404
    assert shell.post("/api/nope", {}).status_code == 404


# ------------------------------------------------------------------ the .meta trap
#
# Publish bookkeeping is a HIDDEN DIRECTORY INSIDE PROJECTS_ROOT, so it sits in the same
# namespace the active project is chosen from, and "." sorts before every letter and
# digit. These two tests exist because it was reachable: delete the active project after
# any publish and the workshop adopted ".meta" as the project — a name absent from its own
# menu, whose contents /preview served and whose "Start over" wiped every project's
# published fingerprint.

def _seed_meta(root: Path):
    (root / ".meta").mkdir(exist_ok=True)
    (root / ".meta" / "alpha.json").write_text('{"published_fp":"aaa"}\n')
    (root / ".meta" / "beta.json").write_text('{"published_fp":"bbb"}\n')


def test_deleting_the_active_project_lands_on_a_real_project(shell):
    (shell.root / "beta").mkdir()
    _seed_meta(shell.root)

    r = shell.post("/api/delete", {"name": "alpha"})
    assert r.status_code == 200, r.text
    assert r.json()["project"] == "beta"

    state = shell.get("/api/state").json()
    assert state["project"] == "beta"
    # The chip must never show a project the menu does not contain.
    assert state["project"] in {p["name"] for p in state["projects"]}
    # .active is repaired, not left naming a directory that is gone — the terminal reads
    # this file directly and mkdir -p's it, so a stale pointer resurrects the deletion.
    assert (shell.root / ".active").read_text().strip() == "beta"
    shell.wait_for(lambda p: p["project"] == "beta", "the sampler to land on a real project")

    # And the other project's publish bookkeeping is still there for the share button.
    assert (shell.root / ".meta" / "beta.json").is_file()


def test_a_stale_active_pointer_never_selects_a_hidden_directory(shell):
    (shell.root / "beta").mkdir()
    _seed_meta(shell.root)
    # What an impatient `rm -rf` from the terminal looks like from in here.
    import shutil
    shutil.rmtree(shell.root / "alpha")

    assert shell.get("/api/state").json()["project"] == "beta"
    p = shell.wait_for(lambda p: p["project"] != "alpha", "the sampler to move off the gap")
    assert p["project"] == "beta"

    # The decisive consequence: "Start over" must not be able to empty the bookkeeping.
    assert shell.post("/api/reset", {}).status_code == 200
    assert (shell.root / ".meta" / "alpha.json").is_file()
    assert (shell.root / ".meta" / "beta.json").is_file()


# ------------------------------------------------------------------ sampler cost

def test_offline_refs_follows_edits_to_index(shell):
    """The count is cached on (mtime_ns, size) so a multi-megabyte inlined page is not
    re-read and re-scanned every window. This proves the cache still tracks the file."""
    index = shell.root / "alpha" / "index.html"
    index.write_text('<script src="https://cdn.example.com/a.js"></script>')
    shell.wait_for(lambda p: p["offline_refs"] == 1, "the first CDN reference")

    index.write_text('<script src="https://a.example.com/a.js"></script>'
                     '<img src="http://b.example.com/b.png">')
    shell.wait_for(lambda p: p["offline_refs"] == 2, "the count to follow an edit")

    index.write_text("<h1>all mine</h1>")
    shell.wait_for(lambda p: p["offline_refs"] == 0, "the count to follow a fix")

    index.unlink()
    shell.wait_for(lambda p: p["offline_refs"] == 0 and not p["has_index"], "the file going away")


def test_aider_run_as_a_console_script_is_seen_as_the_agent(shell, tmp_path):
    """aider is a pip console script, so the kernel execs it through its shebang and
    /proc/<pid>/cmdline reads "<interpreter> /usr/local/bin/aider". Matching only argv[0]
    meant the whole WS_AGENT=aider path could never read as busy."""
    fake = tmp_path / "aider"
    fake.write_text(f"#!{sys.executable}\n"
                    "import time\n"
                    "end = time.time() + 3.0\n"
                    "while time.time() < end: pass\n")
    fake.chmod(0o755)

    proc = subprocess.Popen([str(fake)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        # cmdline is EMPTY for the window between fork and the shebang exec completing.
        # Reading it once raced and gave an IndexError roughly one run in three.
        deadline = time.monotonic() + TIMEOUT
        argv = []
        while time.monotonic() < deadline and not argv:
            try:
                with open(f"/proc/{proc.pid}/cmdline", "rb") as fh:
                    argv = [a.decode() for a in fh.read().split(b"\0") if a]
            except OSError:
                pass
            if not argv:
                time.sleep(0.02)
        assert argv, "the fake aider never got as far as having a command line"
        assert os.path.basename(argv[0]) != "aider", (
            "the premise of this test is gone: the console script no longer reports its "
            f"interpreter in argv[0] — cmdline was {argv}")
        shell.wait_for(lambda p: p["busy"] is True, "aider to read as working")
    finally:
        proc.kill()
        proc.wait(timeout=TIMEOUT)


def test_the_server_refuses_to_start_without_a_token(tmp_path):
    """An unauthenticated shell API on the pod network is worse than no workshop.

    The pod stopped being loopback-only when the workshop became a tab in the portal, so
    this token is the control that survives a NetworkPolicy mistake. Coming up without
    one must stop the process outright, not log a warning nobody reads.
    """
    root = tmp_path / "projects"
    (root / "alpha").mkdir(parents=True)
    (root / ".active").write_text("alpha\n")
    env = {**os.environ, "WS_PROJECTS_ROOT": str(root),
           "WS_SHELL_PORT": str(_free_port()), "WS_USER": "tester"}
    env.pop("WS_INTERNAL_TOKEN", None)
    proc = subprocess.run([sys.executable, str(SERVER)], env=env,
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode != 0, "the server started with no token configured"
    assert "WS_INTERNAL_TOKEN" in (proc.stderr + proc.stdout), (
        f"it refused, but not in a way that says why: {proc.stderr[-300:]}"
    )


def test_every_route_refuses_a_request_with_no_token(shell):
    """Reaching the port must not be the same as using it."""
    for path in ("/", "/api/state", "/api/pulse", "/static/app.js", "/preview/"):
        r = httpx.get(shell.base + path, timeout=TIMEOUT)
        assert r.status_code == 403, f"{path} answered {r.status_code} without a token"
    r = httpx.post(shell.base + "/api/publish", json={}, timeout=TIMEOUT)
    assert r.status_code == 403, f"publish answered {r.status_code} without a token"


def test_a_wrong_token_is_refused(shell):
    r = httpx.get(shell.base + "/api/state", timeout=TIMEOUT,
                  headers={"X-Workspace-Token": "not-the-token"})
    assert r.status_code == 403


def test_a_static_page_is_not_flagged_as_keeps_running(shell):
    (shell.root / "alpha" / "index.html").write_text("<h1>hello</h1>")
    p = shell.wait_for(lambda s: s["has_index"], "index.html to appear")
    assert p["keeps_running"] is False


def test_an_animating_page_is_flagged(shell):
    """The signal that decides whether a child is nudged into a separate tab.

    A page that only paints once cannot make a tab unresponsive. One holding an animation
    loop can, and when it does the browser puts up its own alarming dialog.
    """
    for markup in (
        "<canvas id=c></canvas>",
        "<script>requestAnimationFrame(function f(){requestAnimationFrame(f)})</script>",
        "<script>setInterval(()=>{}, 16)</script>",
        "<script>while(true){}</script>",
    ):
        (shell.root / "alpha" / "index.html").write_text(f"<html>{markup}</html>")
        p = shell.wait_for(lambda s: s["keeps_running"] is True,
                           f"keeps_running for {markup[:24]}")
        assert p["keeps_running"] is True


def test_asking_for_a_fresh_chat_leaves_a_flag_for_the_next_connection(shell):
    """The switch cannot be applied to a running agent.

    ttyd gives every websocket its own shell, so a new session begins when the client
    reconnects. The endpoint's whole job is to leave a note for that next shell.
    """
    project = shell.get("/api/state").json()["project"]
    flag = shell.root / ".meta" / f"{project}.new-session"
    assert not flag.exists()
    r = shell.post("/api/session/new", {})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert flag.is_file(), "no flag was left for the next terminal connection"


def test_the_fresh_chat_flag_is_per_project(shell):
    """Asking for a clean agent in one project must not reset another."""
    shell.post("/api/projects", {"name": "second"})
    shell.post("/api/session/new", {})
    assert (shell.root / ".meta" / "second.new-session").is_file()
    assert not (shell.root / ".meta" / "alpha.new-session").exists()


def test_the_fresh_chat_flag_is_not_visible_to_the_agent_or_the_preview(shell):
    """.meta is ours. It must never be scanned, served or published with a child's work."""
    shell.post("/api/session/new", {})
    assert shell.get("/preview/.meta/").status_code in (403, 404)
    p = shell.pulse()
    assert all(not c.startswith(".meta") for c in p["changed"]), p["changed"]
