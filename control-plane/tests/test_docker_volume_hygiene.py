"""Enterpriseaiframework-fbe: the suite must not leak anonymous Docker volumes.

Images such as postgres, mongo and mailpit declare VOLUME, so every `docker run` of one
creates an anonymous volume. `docker run --rm` removes it on exit, but an explicit
`docker rm -f <name>` WITHOUT -v (what every fixture teardown used) leaves it behind:
~200 volumes / ~9GB a day of full-suite runs.

Identity, not count: other agents share this daemon and create volumes concurrently, so a
before/after count of `docker volume ls -f dangling=true` is racy. Each test resolves the
64-hex anonymous volume that ITS OWN container mounted and asserts that exact volume is
gone after teardown. Shared named volumes are never touched.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import textwrap
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
HEX64 = re.compile(r"^[0-9a-f]{64}$")
IMAGE = "postgres:16-alpine"
SCAN_DIRS = ("tests", "tests-live", "control-plane/tests")


def _docker(*args, timeout=120):
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


def _require_docker():
    if shutil.which("docker") is None or _docker("info").returncode != 0:
        pytest.fail("docker is required: volume hygiene is proven against a real daemon")
    if _docker("image", "inspect", IMAGE).returncode != 0:
        pytest.fail(f"{IMAGE} not present locally; pull it (this test never pulls)")


def _anon_volumes(container: str) -> list[str]:
    r = _docker("inspect", "-f", "{{range .Mounts}}{{.Name}}\n{{end}}", container)
    return [v for v in r.stdout.split() if HEX64.match(v)]


def _volume_exists(name: str) -> bool:
    return _docker("volume", "inspect", name).returncode == 0


def _start(name: str, *extra: str) -> list[str]:
    r = _docker("run", "-d", *extra, "--name", name, "-e", "POSTGRES_PASSWORD=x", IMAGE)
    assert r.returncode == 0, r.stderr
    vols = _anon_volumes(name)
    assert vols, "postgres image should have created an anonymous volume"
    return vols


def test_rm_f_without_v_leaks_and_with_v_does_not():
    """Control, both ways: proves the probe can see the leak, and that -v is the cure.
    Uses a container WITHOUT --rm so the outcome is deterministic (no autoremove race)."""
    _require_docker()
    leaked: list[str] = []
    try:
        bad = f"eaf-fbe-bad-{uuid.uuid4().hex[:8]}"
        vols = _start(bad)
        _docker("rm", "-f", bad)  # the defect: no -v
        leaked = [v for v in vols if _volume_exists(v)]
        assert leaked, "expected `docker rm -f` without -v to leak the anonymous volume"

        good = f"eaf-fbe-good-{uuid.uuid4().hex[:8]}"
        vols = _start(good)
        _docker("rm", "-f", "-v", good)
        assert not [v for v in vols if _volume_exists(v)]
    finally:
        for v in leaked:  # only the volume this test itself deliberately leaked
            _docker("volume", "rm", "-f", v)


def test_real_disposable_postgres_fixture_removes_its_volume(tmp_path):
    """Run the actual control-plane fixture (test_freerouter_mirror.postgres) and check the
    anonymous volume its container mounted is gone after fixture teardown."""
    _require_docker()
    probe = tmp_path / "test_probe_fixture.py"
    out = tmp_path / "volumes.txt"
    probe.write_text(textwrap.dedent(f"""
        import re, subprocess
        from test_freerouter_mirror import postgres  # noqa: F401  (the real fixture)

        def test_probe(postgres):
            port = postgres.rsplit(":", 1)[1].split("/")[0]
            name = subprocess.run(
                ["docker", "ps", "-q", "--filter", f"publish={{port}}"],
                capture_output=True, text=True).stdout.split()[0]
            vols = subprocess.run(
                ["docker", "inspect", "-f", "{{{{range .Mounts}}}}{{{{.Name}}}} {{{{end}}}}", name],
                capture_output=True, text=True).stdout.split()
            open({str(out)!r}, "w").write(
                "\\n".join(v for v in vols if re.fullmatch("[0-9a-f]{{64}}", v)))
    """))
    r = subprocess.run(
        [sys.executable, "-m", "pytest", str(probe), "-q", "-p", "no:cacheprovider",
         "--rootdir", str(tmp_path)],
        capture_output=True, text=True, timeout=300, cwd=REPO / "control-plane",
        env={**os.environ, "PYTHONPATH": str(REPO / "control-plane" / "tests")},
    )
    assert r.returncode == 0, r.stdout + r.stderr
    vols = out.read_text().split()
    try:
        assert vols, "probe saw no anonymous volume; the check would be vacuous"
        leaked = [v for v in vols if _volume_exists(v)]
        assert not leaked, f"fixture teardown leaked anonymous volumes: {leaked}"
    finally:
        for v in vols:
            _docker("volume", "rm", "-f", v)


def _py_docker_rm_calls():
    for d in SCAN_DIRS:
        for p in sorted((REPO / d).rglob("*.py")):
            if p == Path(__file__).resolve():
                continue
            for i, line in enumerate(p.read_text().splitlines(), 1):
                if re.search(r"""["']docker["']\s*,\s*["']rm["']""", line):
                    yield p, i, line


def test_every_docker_rm_in_the_suite_removes_volumes():
    """Static guard against a new fixture re-introducing the leak. Supplementary: the
    behavioural tests above are the proof; this catches the un-exercised call sites."""
    calls = list(_py_docker_rm_calls())
    assert calls, "scanner found no docker rm call sites; the guard is vacuous"
    bad = [f"{p.relative_to(REPO)}:{i}: {l.strip()}" for p, i, l in calls
           if not re.search(r"""["'](-[a-z]*v[a-z]*|--volumes)["']""", l)]
    assert not bad, "docker rm without -v leaks anonymous volumes:\n" + "\n".join(bad)
