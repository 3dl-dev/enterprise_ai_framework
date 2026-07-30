"""Worktree port allocation must avoid the whole host, not just other enterprise-ai stacks.

enterpriseaiframework-7b3: the path-hash offset in render-env.sh (enterpriseaiframework-0e3)
only proves two enterprise-ai checkouts cannot collide WITH EACH OTHER — no pairwise
difference between the six base ports is a multiple of 100, so any two multiple-of-100
offsets are mutually safe. It says nothing about the rest of the host.

OBSERVED: a worktree hashed to GATEWAY_PORT=10000, which azurite-galtrader already held.
`make up` died mid-sequence at "Bind for 0.0.0.0:10000 failed: port is already allocated"
with core services half-started — not a clean refusal. This host also runs se-server and
a 13-pod k3s cluster, so "the range is ours" does not hold here.

Fix: the hash is now the STARTING bucket of a search. render-env.sh probes the six ports a
bucket would produce and, on any collision, walks to the next bucket (wrapping through all
200) until a free block is found, or fails loudly — before `docker compose up` runs at all.

CRITICAL INTERACTION: set_var overwrites the port block on every render, deliberately. If
the probe ran unconditionally, re-rendering while THIS checkout's OWN stack is already up
would see its own containers holding those ports, treat that as a collision, and walk a
live stack's ports out from under itself. The fix special-cases that: if this checkout's
own compose project already has running containers, the offset already recorded in .env is
kept rather than re-probed.

No docker containers are started by any test here — these run the real render-env.sh
against throwaway git repos, exactly like test_bundle_isolation.py, plus a real bind on
127.0.0.1 to simulate a colliding host service, and a stub `docker` on PATH to simulate an
already-running stack without needing real containers.
"""

import contextlib
import os
import shutil
import socket
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BUNDLE = REPO / "bundle"
RENDER = BUNDLE / "bin" / "render-env.sh"

DEFAULT_PORTS = [
    "GATEWAY_PORT", "CONTROL_PLANE_PORT", "IDP_PORT",
    "IDP_HTTPS_PORT", "FAKEPROVIDER_PORT", "CHAT_PORT",
]


def _git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _read_env(env_path: Path) -> dict:
    out = {}
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


def _make_worktree(tmp_path) -> Path:
    """A linked worktree, exactly like test_bundle_isolation.py's fixture: the primary
    checkout keeps 'enterprise-ai'; only a linked worktree exercises the offset-search
    branch this item changes."""
    primary = tmp_path / "primary"
    (primary / "bundle" / "bin").mkdir(parents=True)
    (primary / "bundle" / "keycloak").mkdir(parents=True)
    shutil.copy2(RENDER, primary / "bundle" / "bin" / "render-env.sh")
    shutil.copy2(BUNDLE / ".env.example", primary / "bundle" / ".env.example")
    shutil.copy2(
        BUNDLE / "keycloak" / "realm-export.template.json",
        primary / "bundle" / "keycloak" / "realm-export.template.json",
    )
    _git("init", "-q", cwd=primary)
    _git("config", "user.email", "test@example.com", cwd=primary)
    _git("config", "user.name", "test", cwd=primary)
    _git("add", "-A", cwd=primary)
    _git("commit", "-qm", "bundle", cwd=primary)

    worktree = tmp_path / "wt"
    _git("worktree", "add", "-q", str(worktree), "-b", "wt-branch", cwd=primary)
    return worktree


def _clean_env() -> dict:
    env = {**os.environ}
    for k in [*DEFAULT_PORTS, "COMPOSE_PROJECT_NAME"]:
        env.pop(k, None)
    return env


def _render(cwd, env) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bundle/bin/render-env.sh"], cwd=cwd, capture_output=True, text=True, env=env,
    )


@contextlib.contextmanager
def held_port(port: int):
    """Simulate some other host service already bound to `port`, exactly like
    azurite-galtrader holding 10000 in the observed incident."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", port))
    s.listen(1)
    try:
        yield
    finally:
        s.close()


def _require_openssl():
    if shutil.which("openssl") is None:
        pytest.skip("openssl not available; render-env.sh cannot generate secrets")


def test_a_pre_bound_host_port_forces_a_move_or_a_loud_refusal(tmp_path):
    """The test that fails before the fix.

    Before enterpriseaiframework-7b3, the offset was a pure function of the checkout's
    path hash: whatever a foreign process already had bound on the host was invisible to
    render-env.sh, so it would write the colliding port unconditionally, and `make up`
    would die mid-sequence (observed: GATEWAY_PORT=10000 vs. azurite-galtrader).

    This reproduces exactly that: learn the offset this checkout would naturally get with
    nothing in its way, then re-render from scratch with that exact GATEWAY_PORT already
    bound by someone else. The done condition is either — the six ports the second render
    picked exclude the bound one entirely (moved), or render-env.sh exited non-zero and
    wrote nothing (refused loudly) — never a silent repeat of the colliding value.
    """
    _require_openssl()
    worktree = _make_worktree(tmp_path)
    env = _clean_env()

    baseline = _render(worktree, env)
    assert baseline.returncode == 0, baseline.stderr
    natural = _read_env(worktree / "bundle" / ".env")
    natural_gateway_port = int(natural["GATEWAY_PORT"])

    # Reset to a pristine, never-rendered checkout — the exact state `make up` starts
    # from on a brand-new worktree.
    (worktree / "bundle" / ".env").unlink()

    with held_port(natural_gateway_port):
        contested = _render(worktree, env)

    if contested.returncode != 0:
        # Refused loudly, before anything downstream (make-certs.sh, docker compose up)
        # could run on half-chosen ports. Acceptable per the done condition.
        return

    result = _read_env(worktree / "bundle" / ".env")
    chosen_ports = {int(result[v]) for v in DEFAULT_PORTS}
    assert natural_gateway_port not in chosen_ports, (
        f"render-env.sh re-wrote the colliding port {natural_gateway_port} even though "
        "it was already bound by another host service — this is the exact defect that "
        "left `make up` half-started against azurite-galtrader's port 10000"
    )
    # Sanity: it actually derived a real, internally-consistent block, not a fluke.
    assert len(chosen_ports) == len(DEFAULT_PORTS), "duplicate ports in the moved block"


def test_pre_bound_port_search_is_deterministic_across_repeated_fresh_renders(tmp_path):
    """Same checkout, same (colliding) host state, no running stack of its own: the search
    must land on the same block twice, or debugging a collision gets much harder."""
    _require_openssl()
    worktree = _make_worktree(tmp_path)
    env = _clean_env()

    baseline = _render(worktree, env)
    assert baseline.returncode == 0, baseline.stderr
    natural_gateway_port = int(_read_env(worktree / "bundle" / ".env")["GATEWAY_PORT"])
    (worktree / "bundle" / ".env").unlink()

    with held_port(natural_gateway_port):
        (worktree / "bundle" / ".env").unlink(missing_ok=True)
        first = _render(worktree, env)
        assert first.returncode == 0, first.stderr
        first_ports = _read_env(worktree / "bundle" / ".env")
        (worktree / "bundle" / ".env").unlink()
        second = _render(worktree, env)
        assert second.returncode == 0, second.stderr
        second_ports = _read_env(worktree / "bundle" / ".env")

    for v in DEFAULT_PORTS:
        assert first_ports[v] == second_ports[v], (
            f"{v} differed across two fresh renders of the same checkout against the "
            "same host collision — the search is not deterministic"
        )


def test_no_collision_case_is_unchanged_multiple_of_100_in_range(tmp_path):
    """The case this item did NOT change: with nothing else on the host contesting the
    ports, the offset must still be exactly what enterpriseaiframework-0e3 guaranteed — a
    multiple of 100 in [100, 20000], so the cross-checkout collision-freedom argument and
    the k3s NodePort clearance both still hold."""
    _require_openssl()
    worktree = _make_worktree(tmp_path)
    env = _clean_env()

    proc = _render(worktree, env)
    assert proc.returncode == 0, proc.stderr
    result = _read_env(worktree / "bundle" / ".env")
    gateway = int(result["GATEWAY_PORT"])
    offset = gateway - 4000
    assert offset % 100 == 0, f"offset {offset} is not a multiple of 100"
    assert 100 <= offset <= 20000, f"offset {offset} outside [100, 20000]"
    for v in DEFAULT_PORTS:
        assert (int(result[v]) - offset) in (4000, 8081, 8082, 8443, 8090, 3080), (
            f"{v}={result[v]} is not base+offset for offset {offset}"
        )


def _stub_docker(tmp_path: Path, project_name: str) -> Path:
    """A fake `docker` that reports `project_name` as having a running container for
    `docker compose -p <project_name> ps --status running -q`, and nothing for any other
    project — without needing to start a single real container."""
    bin_dir = tmp_path / "stub-bin"
    bin_dir.mkdir(exist_ok=True)
    docker_stub = bin_dir / "docker"
    docker_stub.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"target={project_name!r}\n"
        'if [[ "$1" == "compose" ]]; then\n'
        '    shift\n'
        '    project=""\n'
        '    while [[ $# -gt 0 ]]; do\n'
        '        if [[ "$1" == "-p" ]]; then project="$2"; fi\n'
        '        shift\n'
        '    done\n'
        '    if [[ "$project" == "$target" ]]; then\n'
        '        echo fake-container-id\n'
        '        exit 0\n'
        '    fi\n'
        '    exit 0\n'
        "fi\n"
        'exit 1\n'
    )
    docker_stub.chmod(docker_stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return bin_dir


def test_rerender_does_not_move_a_running_stacks_own_ports(tmp_path):
    """The critical interaction called out in enterpriseaiframework-7b3: set_var
    overwrites the port block on EVERY render, so a naive unconditional probe would see
    its OWN currently-running containers holding those ports, mistake that for a
    collision, and walk a live stack's ports out from under it on the next `make up`.

    Simulated without real containers: bind the checkout's own already-chosen ports (as
    its containers would), stub `docker compose -p <project> ps --status running -q` to
    report that project as running, and re-render. The ports must not move.
    """
    _require_openssl()
    worktree = _make_worktree(tmp_path)
    env = _clean_env()

    baseline = _render(worktree, env)
    assert baseline.returncode == 0, baseline.stderr
    before = _read_env(worktree / "bundle" / ".env")
    project_name = before["COMPOSE_PROJECT_NAME"]
    before_ports = {v: before[v] for v in DEFAULT_PORTS}

    stub_bin = _stub_docker(tmp_path, project_name)
    env_with_stub = {**env, "PATH": f"{stub_bin}:{env.get('PATH', '')}"}

    holders = []
    try:
        for v in DEFAULT_PORTS:
            holders.append(held_port(int(before_ports[v])))
        for h in holders:
            h.__enter__()

        rerendered = _render(worktree, env_with_stub)
        assert rerendered.returncode == 0, rerendered.stderr
    finally:
        for h in reversed(holders):
            h.__exit__(None, None, None)

    after = _read_env(worktree / "bundle" / ".env")
    for v in DEFAULT_PORTS:
        assert after[v] == before_ports[v], (
            f"{v} moved from {before_ports[v]} to {after[v]} on a re-render while the "
            "checkout's own stack (per the stubbed docker compose ps) was already "
            "running on that exact port — a live stack's ports must not move under it"
        )
