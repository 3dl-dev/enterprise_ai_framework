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

The fake agent is a python-shebang script rather than a bash one on purpose: opencode
ships as a standalone binary in the real image (argv[0] IS "opencode"), but the guard's
fallback branch — the one that also matches a python-interpreter's argv[1] — is the same
branch shell-server.py's own AGENT_NAMES scan already uses for both names symmetrically.
Exercising that branch here proves it actually works, not just that it parses.
"""

import os
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
