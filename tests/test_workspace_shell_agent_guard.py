"""Hermetic tests for the one-agent-per-project guard in deploy/workspace/workspace-shell.

No cluster, no docker, no network, no bundle — the real bash script is run as a
subprocess (the thing that actually executes in the pod, not a description of it),
against a throwaway WS_PROJECTS_ROOT, with a fake `opencode` on PATH standing in for the
real binary. Only the launch decision is under test; the fake never does real agent work.

ITEM (enterpriseaiframework-3fe): ttyd spawns a fresh shell per websocket connection, and
workspace-shell used to start the coding agent unconditionally in each one — so two
tabs, a flaky reconnect, or a reload while the old connection was still draining could
each add another agent process inside a pod capped at 1 CPU
(deploy/k8s/61-workspace.template.yaml; k3s-worker runs live GPU training, so the limit
is not a tuning knob). dogfood-findings.md finding 33 saw exactly this shape: a context
leak piled a dozen agents into one pod until it answered 429.

Two flavors of double, exercising the guard's TWO detection branches separately:

  * FAKE_OPENCODE (the `rig` fixture below) is a python-shebang script. The kernel execs
    a script's shebang interpreter, so /proc/<pid>/cmdline[0] is "python3" and cmdline[1]
    is the script — this exercises agent_pid_for_project()'s `python*) ... argv[1]`
    fallback branch, the same one shell-server.py's own AGENT_NAMES scan uses for both
    agent names symmetrically.

  * A symlink to /bin/cat (the `rig_real_binary` fixture, further down) is a real ELF
    binary with no shebang — invoked through a PATH lookup as "opencode", the kernel
    execs it directly under argv[0]="opencode", nothing in front. This exercises the
    PRIMARY `case "$argv0" in opencode|aider)` branch, which is what the real opencode
    binary shipped in the image actually hits (it is a standalone executable, not a
    script — confirmed by reading deploy/workspace/Dockerfile's COPY line).

A wave-3 veracity review of an earlier version of this file found that only the fallback
branch was ever exercised; the second fixture and its tests close that gap.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE_SHELL = REPO_ROOT / "deploy" / "workspace" / "workspace-shell"

TIMEOUT = 10.0

FAKE_OPENCODE = """\
#!/usr/bin/python3
# Test double for the real opencode binary. Records who invoked it (argv, which is where
# --continue would show up) and then blocks — exactly what a real interactive agent does
# from workspace-shell's point of view: a long-lived process attached to this connection
# — until told to stop, so the guard has something real to find still running in /proc.
import os
import sys
import time

marker_dir = os.environ["FAKE_AGENT_MARKER_DIR"]
os.makedirs(marker_dir, exist_ok=True)
pid = os.getpid()
with open(os.path.join(marker_dir, f"argv.{pid}"), "w") as fh:
    fh.write("\\n".join(sys.argv[1:]))
open(os.path.join(marker_dir, f"started.{pid}"), "w").close()

stop_file = os.path.join(marker_dir, "stop")
deadline = time.time() + 30
while not os.path.exists(stop_file) and time.time() < deadline:
    time.sleep(0.02)
sys.exit(0)
"""


@pytest.fixture
def rig(tmp_path):
    """A throwaway pod: WS_PROJECTS_ROOT, a fake opencode on PATH, and a helper to spawn
    connections against it. Mirrors what entrypoint.sh sets up before workspace-shell runs."""
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    marker_dir = tmp_path / "markers"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    fake = bin_dir / "opencode"
    fake.write_text(FAKE_OPENCODE)
    fake.chmod(0o755)

    def set_active(name: str) -> None:
        (projects_root / name).mkdir(exist_ok=True)
        (projects_root / ".active").write_text(name + "\n")

    def connect(**extra_env) -> subprocess.Popen:
        env = dict(os.environ)
        env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
        env["WS_PROJECTS_ROOT"] = str(projects_root)
        env["FAKE_AGENT_MARKER_DIR"] = str(marker_dir)
        env["WS_USER"] = "tester"
        env.pop("WS_NO_AUTOSTART", None)
        env.pop("WS_AGENT", None)
        env.update(extra_env)
        return subprocess.Popen(
            ["/bin/bash", str(WORKSPACE_SHELL)],
            env=env, cwd=str(tmp_path),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )

    def started_markers() -> set[str]:
        if not marker_dir.is_dir():
            return set()
        return {p.name for p in marker_dir.iterdir() if p.name.startswith("started.")}

    def wait_for_new_started(before: set[str], timeout: float = TIMEOUT) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            new = started_markers() - before
            if new:
                return sorted(new)[0]
            time.sleep(0.02)
        raise AssertionError(
            f"no new agent started within {timeout}s (had {before}, "
            f"still have {started_markers()})"
        )

    def argv_for(started_name: str) -> str:
        pid = started_name.split(".", 1)[1]
        return (marker_dir / f"argv.{pid}").read_text()

    def stop_all() -> None:
        marker_dir.mkdir(parents=True, exist_ok=True)
        (marker_dir / "stop").touch()

    class Rig:
        pass

    r = Rig()
    r.set_active = set_active
    r.connect = connect
    r.started_markers = started_markers
    r.wait_for_new_started = wait_for_new_started
    r.argv_for = argv_for
    r.stop_all = stop_all
    r.marker_dir = marker_dir
    return r


def test_a_lone_connection_starts_fresh(rig):
    """Control case, unaffected by the guard: one connection, one project, nobody else
    running — the agent must still start exactly as it always did."""
    rig.set_active("solo")
    before = rig.started_markers()
    proc = rig.connect()
    try:
        started = rig.wait_for_new_started(before)
        # First run: no session marker existed yet, so no --continue and no --model
        # (no opencode.json in the fresh project dir).
        assert rig.argv_for(started) == ""
    finally:
        rig.stop_all()
        proc.wait(timeout=TIMEOUT)


def test_second_connection_to_the_same_project_does_not_start_a_second_agent(rig):
    """The core fix. Two connections land on the SAME active project — the shape of a
    second browser tab, or a reconnect racing the old socket's teardown. Only one agent
    process may exist for that project at a time."""
    rig.set_active("shared")
    before = rig.started_markers()
    proc1 = rig.connect()
    try:
        started1 = rig.wait_for_new_started(before)
        pid1 = started1.split(".", 1)[1]

        # Several more connections land while the first agent is still running — the
        # DONE condition asks for "several connections" and a process count that does
        # not grow without bound, not just a single duplicate.
        for _ in range(3):
            before_dup = rig.started_markers()
            proc2 = rig.connect()
            out, _ = proc2.communicate(timeout=TIMEOUT)
            assert proc2.returncode == 0
            assert "already running" in out
            assert pid1 in out
            # No new agent process for this project — the process count did not grow.
            assert rig.started_markers() == before_dup
    finally:
        rig.stop_all()
        proc1.wait(timeout=TIMEOUT)


def test_a_different_project_still_gets_its_own_agent(rig):
    """ASSERT THE CASE THAT DID NOT CHANGE. A pod can have several projects; a user
    working project A in one tab must not be blocked from starting project B in another
    — the guard is per-project, not a pod-wide singleton. If this regresses, the fix
    traded a real bug for a worse one (a pod that runs at most one agent, period)."""
    rig.set_active("proj-a")
    before_a = rig.started_markers()
    proc_a = rig.connect()
    try:
        started_a = rig.wait_for_new_started(before_a)
        pid_a = started_a.split(".", 1)[1]

        rig.set_active("proj-b")
        before_b = rig.started_markers()
        proc_b = rig.connect()
        started_b = rig.wait_for_new_started(before_b)
        pid_b = started_b.split(".", 1)[1]

        assert pid_a != pid_b
        # Both are still alive at once — two real agents, one per project, in one pod.
        assert rig.started_markers() >= {started_a, started_b}
    finally:
        rig.stop_all()
        proc_a.wait(timeout=TIMEOUT)
        proc_b.wait(timeout=TIMEOUT)


def test_reconnect_after_the_agent_exits_resumes_rather_than_refusing(rig):
    """DONE condition (2): once the running agent actually exits, the guard must not
    wedge the project shut — the next connection starts a new one, and because the
    existing `.session` marker mechanism is untouched by the guard, it resumes
    (--continue) instead of opening a blank session."""
    rig.set_active("resume-me")
    before = rig.started_markers()
    proc1 = rig.connect()
    started1 = rig.wait_for_new_started(before)
    rig.stop_all()
    proc1.wait(timeout=TIMEOUT)

    before2 = rig.started_markers()
    proc2 = rig.connect()
    started2 = rig.wait_for_new_started(before2)
    proc2.wait(timeout=TIMEOUT)

    assert started2 != started1
    assert "--continue" in rig.argv_for(started2).split()


# ---------------------------------------------------------------------------------------
# PRIMARY DETECTION PATH — argv[0] IS "opencode", no interpreter in front of it.
#
# A wave-3 veracity review of this file's first version found a real coverage gap: the
# FAKE_OPENCODE double above is a python-shebang script. When the kernel execs a script,
# it execs the INTERPRETER named in the shebang line, so /proc/<pid>/cmdline[0] is
# "/usr/bin/python3" and cmdline[1] is the script path — every test above this line
# therefore only ever exercises agent_pid_for_project()'s `python*) ... argv[1]` fallback
# branch. The PRIMARY branch — `case "$argv0" in opencode|aider)`, which is what fires
# against the real opencode binary shipped in the image (a standalone executable, not a
# script; confirmed by reading deploy/workspace/Dockerfile's COPY line, not asserted) —
# was never once hit by any test.
#
# Fixed here without adding a compiler dependency to the test suite: /bin/cat is a real
# ELF binary with no shebang (verified: `file /bin/cat` on the machine that ran this
# fix). A symlink named "opencode" pointing at it, invoked through a PATH lookup, makes
# the kernel exec cat's ELF image directly under argv[0]="opencode" — no interpreter
# step, exactly the shape of the real binary. `cat` with zero arguments blocks reading
# its own stdin, which stands in for "a real long-lived agent still attached to its
# connection" as long as the test keeps that stdin pipe open. The project directories
# below are always fresh (no .session marker, no opencode.json), so workspace-shell
# invokes the double as a bare `opencode` with no arguments — cat never sees a flag it
# would reject.
# ---------------------------------------------------------------------------------------


def _live_pids_matching_argv0(target_dir: str, name: str) -> set[str]:
    """Independent ground truth, not a call into the guard under test: scan /proc
    ourselves for a process whose cwd is target_dir and whose argv[0] basename is
    exactly `name` with nothing in front of it (the primary-branch shape). If this
    ever found matches via an interpreter's argv[1] instead, it would not be testing
    what this section claims to test — so, deliberately, it only ever looks at argv[0].
    """
    pids = set()
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            cwd = os.path.realpath(f"/proc/{entry}/cwd")
            if cwd != target_dir:
                continue
            with open(f"/proc/{entry}/cmdline", "rb") as fh:
                cmdline = fh.read()
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        argv0 = cmdline.split(b"\x00", 1)[0]
        if os.path.basename(argv0.decode(errors="replace")) == name:
            pids.add(entry)
    return pids


@pytest.fixture
def rig_real_binary(tmp_path):
    """Same shape as `rig`, but the opencode double is a real ELF binary (a symlink to
    /bin/cat) instead of a script, so the guard's primary argv[0]-is-"opencode" branch
    is what actually fires — not the python-interpreter fallback the other fixture
    exercises."""
    cat_path = shutil.which("cat") or "/bin/cat"
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    os.symlink(cat_path, bin_dir / "opencode")

    def set_active(name: str) -> str:
        project_dir = projects_root / name
        project_dir.mkdir(exist_ok=True)
        (projects_root / ".active").write_text(name + "\n")
        return os.path.realpath(str(project_dir))

    def connect_blocking() -> subprocess.Popen:
        """Starts a connection whose agent double blocks (reading its own stdin, kept
        open) until `release` closes that pipe — standing in for an agent still
        attached to a live connection."""
        env = dict(os.environ)
        env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
        env["WS_PROJECTS_ROOT"] = str(projects_root)
        env["WS_USER"] = "tester"
        env.pop("WS_NO_AUTOSTART", None)
        env.pop("WS_AGENT", None)
        return subprocess.Popen(
            ["/bin/bash", str(WORKSPACE_SHELL)],
            env=env, cwd=str(tmp_path),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )

    def connect_refused() -> subprocess.Popen:
        """A connection that is expected to be refused by the guard before it ever
        tries to launch an agent — safe to use a closed/throwaway stdin for these."""
        env = dict(os.environ)
        env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
        env["WS_PROJECTS_ROOT"] = str(projects_root)
        env["WS_USER"] = "tester"
        env.pop("WS_NO_AUTOSTART", None)
        env.pop("WS_AGENT", None)
        return subprocess.Popen(
            ["/bin/bash", str(WORKSPACE_SHELL)],
            env=env, cwd=str(tmp_path),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )

    def wait_live(project_dir: str, count: int, timeout: float = TIMEOUT) -> set[str]:
        deadline = time.time() + timeout
        pids: set[str] = set()
        while time.time() < deadline:
            pids = _live_pids_matching_argv0(project_dir, "opencode")
            if len(pids) >= count:
                return pids
            time.sleep(0.02)
        raise AssertionError(
            f"expected {count} live opencode(argv0) process(es) under {project_dir} "
            f"within {timeout}s, found {sorted(pids)}"
        )

    def release(proc: subprocess.Popen) -> None:
        # Closing our end of the stdin pipe delivers EOF to cat (and to whatever the
        # script execs afterwards), which is what makes the double — and then the
        # wrapping shell — actually exit instead of hanging the test.
        try:
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        proc.wait(timeout=TIMEOUT)

    class Rig:
        pass

    r = Rig()
    r.set_active = set_active
    r.connect_blocking = connect_blocking
    r.connect_refused = connect_refused
    r.wait_live = wait_live
    r.live_pids = _live_pids_matching_argv0
    r.release = release
    return r


def test_primary_branch_refuses_a_second_real_binary_connection(rig_real_binary):
    """The gap wave-3 named: with a REAL binary double (argv[0] literally "opencode",
    no interpreter), a second connection to the same project must still be refused, and
    the live process count for that project directory must stay at 1."""
    r = rig_real_binary
    project_dir = r.set_active("shared-real")
    proc1 = r.connect_blocking()
    try:
        live = r.wait_live(project_dir, 1)
        assert len(live) == 1
        pid1 = next(iter(live))

        for _ in range(3):
            proc2 = r.connect_refused()
            out, _ = proc2.communicate(timeout=TIMEOUT)
            assert proc2.returncode == 0
            assert "already running" in out
            assert pid1 in out
            # Ground truth via our OWN /proc scan, independent of the guard's message:
            # still exactly one live process for this project.
            assert r.live_pids(project_dir, "opencode") == {pid1}
    finally:
        r.release(proc1)


def test_primary_branch_pre_fix_script_lets_the_duplicate_through(tmp_path):
    """DEMONSTRATED, not asserted: run the identical scenario above against the
    PRE-FIX script (the parent commit, before this guard existed) and show the process
    count for one project actually grows past 1 — the amplification finding-33 shape,
    reproduced through the real-binary double this time, not just the python one."""
    pre_fix_script = tmp_path / "workspace-shell-pre-fix"
    content = subprocess.run(
        ["git", "show", "8c4ffd2:deploy/workspace/workspace-shell"],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=TIMEOUT, check=True,
    ).stdout
    assert "agent_pid_for_project" not in content, (
        "picked a commit that already has the guard — this test would prove nothing"
    )
    pre_fix_script.write_text(content)
    pre_fix_script.chmod(0o755)

    cat_path = shutil.which("cat") or "/bin/cat"
    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    os.symlink(cat_path, bin_dir / "opencode")

    project_dir = projects_root / "shared-real"
    project_dir.mkdir()
    (projects_root / ".active").write_text("shared-real\n")
    project_dir_real = os.path.realpath(str(project_dir))

    def connect(stdin) -> subprocess.Popen:
        env = dict(os.environ)
        env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
        env["WS_PROJECTS_ROOT"] = str(projects_root)
        env["WS_USER"] = "tester"
        env.pop("WS_NO_AUTOSTART", None)
        env.pop("WS_AGENT", None)
        return subprocess.Popen(
            ["/bin/bash", str(pre_fix_script)],
            env=env, cwd=str(tmp_path),
            stdin=stdin,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )

    proc1 = connect(subprocess.PIPE)
    proc2 = connect(subprocess.PIPE)
    try:
        deadline = time.time() + TIMEOUT
        pids: set[str] = set()
        while time.time() < deadline:
            pids = _live_pids_matching_argv0(project_dir_real, "opencode")
            if len(pids) >= 2:
                break
            time.sleep(0.02)
        # Pre-fix: nothing stopped the second connection from starting its own agent.
        assert len(pids) == 2, (
            f"expected the unfixed script to let a second agent start (finding-33's "
            f"shape); found {sorted(pids)} live for {project_dir_real}"
        )
    finally:
        for p in (proc1, proc2):
            try:
                p.stdin.close()
            except (BrokenPipeError, OSError):
                pass
            p.wait(timeout=TIMEOUT)
