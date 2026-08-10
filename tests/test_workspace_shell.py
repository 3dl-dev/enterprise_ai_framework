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
    """The highest-value line in the server: it turns "your page reaches off itself for a
    piece of itself" into a sentence a child can act on.

    The docstring here used to say "the pod has no egress; a CDN <script src> renders a
    blank page". That was false twice over — see the OFFLINE_REF comment in shell-server.py
    and enterpriseaiframework-644. The counter is a house-rule check, not a capability
    check; what it counts is unchanged."""
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


def test_the_preview_does_not_block_remote_subresources(shell):
    """enterpriseaiframework-644, the second mechanism.

    The retired claim was that a remote <script src> in a child's page "arrives as
    nothing". Even if the pod's NetworkPolicy denied egress — it does not — that would not
    make the claim true, because THIS POD DOES NOT FETCH THE PAGE. The preview is
    `<iframe src="preview/">` (deploy/workspace/shell/app.js), so the child's browser
    fetches index.html from this route and then fetches every subresource named inside it
    over the child's own connection.

    Two things would have to be true for the shell to actually block that, and this test
    measures both against the real server rather than reading the source:

      1. a Content-Security-Policy on the preview response restricting script-src, and
      2. the remote reference not surviving into the served body.

    Neither holds. If a future change adds a CSP here, this test fails and the workshop's
    copy gets to make the offline claim honestly — at which point say so deliberately,
    rather than deleting the assertion to get green.
    """
    (shell.root / "alpha" / "index.html").write_text(
        '<script src="https://cdn.example.com/x.js"></script><h1>mine</h1>'
    )
    r = shell.get("/preview/")
    assert r.status_code == 200

    assert "content-security-policy" not in {k.lower() for k in r.headers}, (
        f"the preview now sends a CSP ({dict(r.headers)}) — check whether it actually "
        "blocks remote subresources, and if it does, the 'no internet' copy retired by "
        "enterpriseaiframework-644 may be worth reinstating as a true statement"
    )
    assert b'src="https://cdn.example.com/x.js"' in r.content, (
        "the server rewrote or stripped the remote reference; if it now does that, the "
        "claim that remote references do not load has become true and the workspace text "
        "should say so again"
    )

    # The counter still SEES it — the house-rule check is what survives the correction.
    p = shell.wait_for(lambda p: p["has_index"], "index.html to register")
    assert p["offline_refs"] == 1


def test_preview_without_an_index_explains_itself(shell):
    r = shell.get("/preview/")
    assert r.status_code == 404
    assert b"index.html" in r.content


def test_preview_serves_a_wasm_engine_build(shell):
    """A WebAssembly engine export (Godot, Unity, a hand-rolled emscripten runtime) is only
    reachable through this route, and it only RUNS if the .wasm comes back as exactly
    application/wasm — `WebAssembly.instantiateStreaming` refuses anything else and the game
    is a blank canvas. It also has to serve a multi-file build from subfolders, not just a
    lone root index.html. This is the server half of "you can build a 3D game here".
    """
    game = shell.root / "alpha"
    (game / "index.html").write_text("<canvas id=c></canvas><script src=game.js></script>")
    (game / "game.wasm").write_bytes(b"\x00asm\x01\x00\x00\x00")  # the wasm magic bytes
    (game / "game.pck").write_bytes(b"GDPC\x00\x00")             # a Godot data pack
    (game / "assets").mkdir()
    (game / "assets" / "world.glb").write_bytes(b"glTF\x02")

    w = shell.get("/preview/game.wasm")
    assert w.status_code == 200
    assert w.headers["Content-Type"] == "application/wasm"
    assert w.content.startswith(b"\x00asm")
    # Lets a cross-origin-isolated (threaded) embedder load the module; harmless for the
    # single-threaded default. Its silent absence is exactly how a threaded build fails.
    assert w.headers.get("Cross-Origin-Resource-Policy") == "cross-origin"

    assert shell.get("/preview/game.pck").headers["Content-Type"] == "application/octet-stream"

    nested = shell.get("/preview/assets/world.glb")
    assert nested.status_code == 200
    assert nested.headers["Content-Type"] == "model/gltf-binary"


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


def test_switching_model_also_asks_for_a_fresh_session(shell):
    """opencode pins the model to the session.

    A resumed session comes back on the model it started with — measured directly:
    `opencode --continue --model glm-4.7` paints "GLM 4.7" and then "GLM 5.2" as the
    session loads over it. So writing the config alone changed nothing a user could see,
    which is what "the setting does nothing" was. The change has to end the session too.
    """
    project = shell.get("/api/state").json()["project"]
    flag = shell.root / ".meta" / f"{project}.new-session"
    assert not flag.exists()
    r = shell.post("/api/model", {"model": "glm-4.7@deepinfra"})
    assert r.status_code == 200, r.text
    assert shell.get("/api/state").json()["model"] == "glm-4.7@deepinfra"
    assert flag.is_file(), (
        "the model was written but no fresh session was asked for, so the agent would "
        "resume on the old model and the setting would appear to do nothing"
    )


def test_an_unknown_model_is_refused_and_changes_nothing(shell):
    before = shell.get("/api/state").json()["model"]
    r = shell.post("/api/model", {"model": "definitely-not-a-model"})
    assert r.status_code == 400
    assert shell.get("/api/state").json()["model"] == before


# ------------------------------------------------------------------ booting state
#
# These drive the REAL page in a real browser rather than asserting on the source, because
# what broke was timing, not text: every function involved already existed and every one of
# them ran. Clicking "Start something new" assigned the terminal iframe's .src and fired a
# toast in the same tick, and .src does not wait for load — so the only feedback the user
# got was a message that appeared and expired over a black rectangle while ttyd respawned
# the shell and the agent cold-booted inside it. A source-level test cannot tell that apart
# from the fixed version.
#
# The iframe itself 404s here (ttyd is a separate process and is not running), which is
# fine and is not what is under test: the assertion is about what the workshop paints over
# the terminal while the terminal is not yet there.

BOOT_SETTLE_MS = 2000       # must match app.js
PAGE_TIMEOUT = 15000


@pytest.fixture
def page(shell):
    pw = pytest.importorskip("playwright.sync_api")
    with pw.sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(extra_http_headers={"X-Workspace-Token": TEST_TOKEN})
        pg = ctx.new_page()
        pg.goto(shell.base + "/", timeout=PAGE_TIMEOUT)
        # The menu is rendered by load(), which is async — wait for the real thing rather
        # than for a bare DOM ready that would race it.
        pg.wait_for_selector("#project-menu button.new", state="attached", timeout=PAGE_TIMEOUT)
        yield pg
        ctx.close()
        browser.close()


def _make_project(page, name: str) -> None:
    page.click("#project-button")
    page.click("#project-menu button.new")
    page.fill("#new-name", name)
    page.click("#new-go")


def test_starting_something_new_paints_a_booting_state(page):
    """The reported bug, inverted into an assertion.

    Before the fix there was nothing at all between the dialog closing and the agent's
    first paint. `#booting` must be up essentially immediately — not after a round trip,
    because the round trip is part of what the user is waiting through.
    """
    assert page.is_hidden("#booting")
    _make_project(page, "space cats")
    page.wait_for_selector("#booting", state="visible", timeout=PAGE_TIMEOUT)

    # The overlay outlives the request that raised it and goes on to narrate the reload,
    # so the name has to survive that handover. It did not at first: the second phase
    # reset the text to a generic "Starting the agent…", which is the least useful moment
    # to stop saying what is being started.
    page.wait_for_function(
        """() => {
             const t = document.getElementById('booting-text').textContent;
             return t.includes('space cats') && t.includes('agent');
           }""",
        timeout=PAGE_TIMEOUT,
    )


def test_the_booting_state_does_not_clear_while_no_agent_exists(page, shell):
    """`busy: null` means "no agent process visible", and that is the whole signal.

    The failure this rules out is an overlay cleared by a timer or by the iframe's own load
    event: ttyd's page loads long before the agent inside it does, so clearing on load
    would reproduce the original bug with an extra frame of ceremony. No agent is running
    in this fixture, so the correct behaviour is to keep waiting.
    """
    foreign = _foreign_agents()
    if foreign:
        pytest.skip("an agent is running on this host, so 'no agent' cannot be asserted:\n  "
                    + "\n  ".join(foreign))
    _make_project(page, "quiet project")
    page.wait_for_selector("#booting", state="visible", timeout=PAGE_TIMEOUT)
    # Comfortably past the settle window and several client poll cycles.
    page.wait_for_timeout(BOOT_SETTLE_MS + 2500)
    assert page.is_visible("#booting"), (
        "the booting overlay cleared while /api/pulse still reported busy=null — nothing "
        "had started, so something other than the agent signal cleared it"
    )


def test_the_booting_state_clears_once_an_agent_is_actually_running(page, tmp_path):
    """The other half: a real process named `opencode` makes pulse report non-null busy.

    Same fake-agent mechanism as test_busy_latches_through_all_three_states — a copy of the
    interpreter renamed, so the server's /proc scan sees a genuine agent name rather than a
    stub the test taught it to recognise.
    """
    _make_project(page, "loud project")
    page.wait_for_selector("#booting", state="visible", timeout=PAGE_TIMEOUT)

    fake = tmp_path / "opencode"
    fake.write_bytes(Path(sys.executable).read_bytes())
    fake.chmod(0o755)
    proc = subprocess.Popen([str(fake), "-c", "import time; time.sleep(60)"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        page.wait_for_selector("#booting", state="hidden", timeout=PAGE_TIMEOUT)
    finally:
        proc.kill()
        proc.wait(timeout=TIMEOUT)


def test_a_rejected_name_puts_the_terminal_back(page, shell):
    """A 400 must not strand the user under a spinner for a project that was never made.

    'alpha' already exists in the fixture root, so /api/projects refuses it.
    """
    _make_project(page, "alpha")
    # Wait on the REFUSAL, not on the overlay: `#booting` is hidden before the click is
    # processed as well as after the failure is handled, so waiting for hidden first
    # passes instantly and proves nothing.
    page.wait_for_selector("#toast", state="visible", timeout=PAGE_TIMEOUT)
    assert "alpha" in page.text_content("#toast")
    assert page.is_hidden("#booting"), (
        "a refused name left the booting overlay up — the user is watching a spinner for "
        "a project that was never created"
    )


def test_losing_contact_retracts_the_booting_claim(page):
    """"Starting the agent…" is a claim, and it stops being true when contact is lost.

    The failure bar is the statement that matters once the workshop is unreachable, and it
    is the only one the user can act on. A spinner still asserting progress on top of it
    would be this surface's cardinal sin — narrating something nobody can observe.

    WHAT THIS DOES AND DOES NOT PROVE: the failure bar is revealed directly rather than by
    severing the server, because the client waits 40 s before showing it and a 40 s test
    would be paid for on every run forever. So this exercises the real syncBooting through
    the real 500 ms ticker against the real DOM, but it does not prove the bar itself
    appears — test_the_ribbon_degrades_when_pulse_dies and the failbar tests own that half.
    """
    _make_project(page, "doomed project")
    page.wait_for_selector("#booting", state="visible", timeout=PAGE_TIMEOUT)
    page.eval_on_selector("#bar-lost", "el => el.hidden = false")
    page.wait_for_selector("#booting", state="hidden", timeout=PAGE_TIMEOUT)
