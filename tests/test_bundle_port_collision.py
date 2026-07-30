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

Most tests here run the real render-env.sh against throwaway git repos, exactly like
test_bundle_isolation.py, plus a real bind to simulate a colliding host service. Two
things this second pass adds, because the first pass's suite could not have caught a
regression in either:

1. The offset the no-collision case picks is asserted against an INDEPENDENTLY computed
   path-hash bucket (real `cksum`, not a call into render-env.sh) — not just its shape
   (multiple of 100, in range). A shape-only check cannot tell "started the search at the
   correct hash-derived bucket" apart from "started the search at bucket 0 every time" —
   the second is a real regression (it silently discards the whole point of this item,
   which is that the hash is the STARTING point of a search, not a fixed answer) and would
   still produce a multiple-of-100 offset in range on this checkout's own tests.

2. `port_in_use`'s probe is exercised against the two classes of listener a 127.0.0.1
   TCP-connect probe cannot see at all: one bound only to IPv6 loopback (`::1`), and one
   bound to a single specific address that is not the default loopback address docker
   itself would test against. Real hosts carry both — this one has a real, unrelated
   `kubectl port-forward` on `[::1]:18099` right now, invisible to a 127.0.0.1-connect
   probe — so a fix that only ever tried 127.0.0.1 would look complete against its own
   suite while missing exactly the collisions most likely on a shared, multi-service host.

3. At least one assertion about `own_project_running()` runs against the REAL docker
   daemon rather than the stubbed `docker` on PATH used by the other tests in this file.
   The stub is kept (fast, no docker dependency, exercises the exact `-p <project>`
   argument parsing) but a stub that always answers "yes, running" for whatever project
   name it's given can never expose a defect in the real `docker compose ... ps` call
   itself (wrong flag, wrong exit-code handling, wrong field parsed) — only a real
   invocation can.
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
BASE_PORTS = (4000, 8081, 8082, 8443, 8090, 3080)


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
def held_port(port: int, family=socket.AF_INET, addr="127.0.0.1"):
    """Simulate some other host service already bound to `port`.

    Default (AF_INET, 127.0.0.1) is exactly like azurite-galtrader holding 10000 in the
    observed incident, and is what a TCP-connect probe on 127.0.0.1 could already see.
    `family`/`addr` let callers simulate the two classes a connect-only probe on
    127.0.0.1 is blind to: an IPv6-loopback-only listener (`AF_INET6, "::1"`) and a
    listener on a specific address other than the default loopback address
    (`AF_INET, "127.0.0.2"` — a stand-in for a real interface address; 127.0.0.0/8 is
    entirely loopback on Linux, so this is bindable in a sandbox without owning a real
    NIC address, and a specific-address bind behaves identically to one either way for
    the purpose of proving the probe sees it).
    """
    s = socket.socket(family, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((addr, port))
    s.listen(1)
    try:
        yield
    finally:
        s.close()


def _cksum(s: str) -> int:
    return int(
        subprocess.run(
            ["cksum"], input=s, capture_output=True, text=True, check=True,
        ).stdout.split()[0]
    )


def _natural_offset(worktree: Path) -> int:
    """Independently reproduce render-env.sh's hex_hash/START_BUCKET arithmetic (the
    `hex_hash()` and `START_BUCKET=$(( HASH % 200 ))` / `OFFSET=(bucket+1)*100` lines) using
    the real `cksum` binary directly — NOT by calling into render-env.sh or importing any
    of its logic. This is what makes the no-collision assertions below a genuine
    independent check of the derived STARTING bucket, rather than a restatement of
    whatever the script happens to compute.
    """
    toplevel = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=worktree / "bundle", capture_output=True, text=True, check=True,
    ).stdout.strip()
    start_bucket = _cksum(toplevel) % 200
    return (start_bucket + 1) * 100


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

    Also asserts the POSITIVE half of the same claim: the offset only moves BECAUSE the
    probe found the natural bucket taken, not unconditionally. A regression that started
    every search at bucket 0 regardless of the path hash would also "move" off of
    natural_gateway_port here (bucket 0 is very unlikely to equal the natural bucket), so
    this test alone cannot catch that regression — that is exactly what
    test_no_collision_case_matches_the_path_hash_derived_bucket below is for. The two
    together pin both directions: moves only when it must (this test), and starts from
    the right place when it need not move at all (that one).
    """
    _require_openssl()
    worktree = _make_worktree(tmp_path)
    env = _clean_env()

    natural_offset = _natural_offset(worktree)

    baseline = _render(worktree, env)
    assert baseline.returncode == 0, baseline.stderr
    natural = _read_env(worktree / "bundle" / ".env")
    natural_gateway_port = int(natural["GATEWAY_PORT"])
    assert natural_gateway_port - 4000 == natural_offset, (
        "test setup assumption broken: the baseline (uncontested) render did not land on "
        "the independently-computed path-hash bucket"
    )

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
    chosen_offset = int(result["GATEWAY_PORT"]) - 4000
    assert natural_gateway_port not in chosen_ports, (
        f"render-env.sh re-wrote the colliding port {natural_gateway_port} even though "
        "it was already bound by another host service — this is the exact defect that "
        "left `make up` half-started against azurite-galtrader's port 10000"
    )
    assert chosen_offset != natural_offset, (
        "the six ports excluded the bound one, but the offset used to derive them still "
        "equals the natural path-hash bucket — that combination is impossible unless the "
        "block computed here does not actually correspond to the recorded offset; the "
        "search must have walked away from the natural bucket precisely because the "
        "probe found it taken"
    )
    # Sanity: it actually derived a real, internally-consistent block, not a fluke.
    assert len(chosen_ports) == len(DEFAULT_PORTS), "duplicate ports in the moved block"


def test_no_collision_case_matches_the_path_hash_derived_bucket(tmp_path):
    """The case this item did NOT change, asserted precisely rather than by shape.

    enterpriseaiframework-0e3 guaranteed the offset is a multiple of 100 in [100, 20000]
    (test_no_collision_case_is_unchanged_multiple_of_100_in_range, below, still checks
    that). That shape alone is not enough to prove this item's actual repurposing of the
    hash held: this item turned the hash from THE ANSWER into the STARTING BUCKET of a
    search. A regression that ignored the hash and always started the search at bucket 0
    would still emit a multiple-of-100 offset in range on almost every run — indeed on
    every run where nothing on the host collides with bucket 0's ports, which is the
    common case in CI — and pass a shape-only check while silently discarding the whole
    point of deriving the bucket from the checkout's own path (two different worktrees
    would then both drift toward the same starting bucket instead of getting distinct,
    stable identities). This computes the expected bucket independently (real `cksum`,
    not a call into render-env.sh) and asserts equality outright.
    """
    _require_openssl()
    worktree = _make_worktree(tmp_path)
    env = _clean_env()

    expected_offset = _natural_offset(worktree)

    proc = _render(worktree, env)
    assert proc.returncode == 0, proc.stderr
    result = _read_env(worktree / "bundle" / ".env")
    offset = int(result["GATEWAY_PORT"]) - 4000
    assert offset == expected_offset, (
        f"rendered offset {offset} does not match the path-hash-derived starting bucket "
        f"{expected_offset} — with nothing on the host contesting any port, the search "
        "must land on its first (hash-derived) candidate, not merely on SOME valid "
        "multiple of 100"
    )


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


def test_probe_sees_a_listener_bound_only_to_ipv6_loopback(tmp_path):
    """The class of collision a 127.0.0.1 TCP-connect probe is structurally blind to.

    This host runs a real example right now — `kubectl port-forward` listening on
    `[::1]:18099` and nothing on `127.0.0.1:18099` at all — which a connect-only probe on
    127.0.0.1 would never see, and would then hand that exact port to a worktree as if it
    were free. `port_in_use` has to actually detect this, not merely claim to in a
    comment: this binds one of the checkout's OWN naturally-derived ports to `::1` only
    (never touching 127.0.0.1) and asserts the search moves off of it, exactly like the
    IPv4 azurite case in test_a_pre_bound_host_port_forces_a_move_or_a_loud_refusal.
    """
    _require_openssl()
    worktree = _make_worktree(tmp_path)
    env = _clean_env()

    baseline = _render(worktree, env)
    assert baseline.returncode == 0, baseline.stderr
    natural_gateway_port = int(_read_env(worktree / "bundle" / ".env")["GATEWAY_PORT"])
    (worktree / "bundle" / ".env").unlink()

    with held_port(natural_gateway_port, family=socket.AF_INET6, addr="::1"):
        contested = _render(worktree, env)

    if contested.returncode != 0:
        return  # loud refusal is an acceptable outcome, same as the IPv4 case

    result = _read_env(worktree / "bundle" / ".env")
    chosen_ports = {int(result[v]) for v in DEFAULT_PORTS}
    assert natural_gateway_port not in chosen_ports, (
        f"render-env.sh handed out {natural_gateway_port} even though it was already "
        "bound on [::1] — a connect-only probe on 127.0.0.1 cannot see this class of "
        "listener at all, which is exactly the gap this host demonstrates for real via "
        "kubectl port-forward on [::1]:18099"
    )


def test_probe_sees_a_listener_bound_to_a_single_non_default_address(tmp_path):
    """The other blind spot: a listener bound to one specific address that is not the
    address a connect-only probe happens to test. 127.0.0.2 stands in for a real
    non-loopback interface address (127.0.0.0/8 is entirely loopback on Linux, so this is
    bindable in a sandbox without owning a real NIC address, and the kernel's bind
    conflict semantics for a specific-address holder are the same either way)."""
    _require_openssl()
    worktree = _make_worktree(tmp_path)
    env = _clean_env()

    baseline = _render(worktree, env)
    assert baseline.returncode == 0, baseline.stderr
    natural_gateway_port = int(_read_env(worktree / "bundle" / ".env")["GATEWAY_PORT"])
    (worktree / "bundle" / ".env").unlink()

    with held_port(natural_gateway_port, family=socket.AF_INET, addr="127.0.0.2"):
        contested = _render(worktree, env)

    if contested.returncode != 0:
        return

    result = _read_env(worktree / "bundle" / ".env")
    chosen_ports = {int(result[v]) for v in DEFAULT_PORTS}
    assert natural_gateway_port not in chosen_ports, (
        f"render-env.sh handed out {natural_gateway_port} even though it was already "
        "bound on 127.0.0.2 — a probe hardcoded to test only 127.0.0.1 cannot see a "
        "listener on any other specific address"
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

    This stub always answers "yes, running" for whatever `-p <project>` it's asked about
    (it only distinguishes the target project from every other one) — it forces
    own_project_running()'s TRUE branch by construction rather than by observing a real
    docker daemon's actual answer, so it cannot expose a defect in the real invocation
    itself (a wrong flag, a wrong exit-code convention, a parsing mistake against real
    `docker compose ps` output shape). Kept because it is fast and needs no docker
    dependency to at least prove the `-p <project>` argument is threaded through
    correctly. test_rerender_does_not_move_a_running_stacks_own_ports_against_real_docker
    below exercises the same claim against the actual docker daemon, with no stub.
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


def test_rerender_does_not_move_a_running_stacks_own_ports_against_real_docker(tmp_path):
    """Same claim as test_rerender_does_not_move_a_running_stacks_own_ports, but against
    the REAL docker daemon — no PATH stub anywhere in this test.

    The stub test above sits directly on top of own_project_running(): its fake `docker`
    answers "yes, running" for whatever `-p <project>` it's asked about, purely from a
    bash string compare, so it can never expose a defect in the real invocation itself —
    a wrong flag, a wrong exit-code convention, a mistaken assumption about `docker
    compose ps`'s real output shape. Verified directly (not asserted): `docker compose -p
    <project> ps --status running -q` returns the container ID with exit 0 when that
    project has a running container and empty output with exit 0 when it does not, run
    from a directory with no docker-compose.yml at all — exactly the shape
    own_project_running() depends on and exactly the cwd render-env.sh calls it from.

    Brings up ONE real, minimal container (alpine, `sleep`, no ports published, no
    network needed beyond an already-local image) under the EXACT compose project name
    render-env.sh itself derives for this throwaway worktree. Critically, this ALSO
    externally binds the checkout's own six ports (exactly like the stub test's
    `held_port` calls) before re-rendering: the probe container itself publishes no
    ports, so without that external bind, a real `own_project_running()` defect (wrong
    flag, wrong exit-code handling, wrong field parsed — a fresh probe would then run
    instead of being skipped) would go undetected here too, because a fresh probe would
    simply find those ports free again and land back on the same offset by coincidence.
    With the ports externally held, "unchanged after re-render" is only possible if
    own_project_running() actually made the real docker daemon say "yes, running" and the
    probe was genuinely skipped — verified this discriminates by deliberately breaking
    the real invocation (`--status running` -> a filter value matching no real container
    status) during this fix's own verification and confirming this test fails on that
    injection while the stub test above does not (the stub does not care what flag was
    passed).
    """
    if shutil.which("docker") is None:
        pytest.skip("docker not available")
    _require_openssl()
    worktree = _make_worktree(tmp_path)
    env = _clean_env()

    baseline = _render(worktree, env)
    assert baseline.returncode == 0, baseline.stderr
    before = _read_env(worktree / "bundle" / ".env")
    project_name = before["COMPOSE_PROJECT_NAME"]
    before_ports = {v: before[v] for v in DEFAULT_PORTS}

    compose_file = tmp_path / "probe-compose.yml"
    compose_file.write_text(
        "services:\n"
        "  probe:\n"
        "    image: alpine:3\n"
        '    command: ["sleep", "120"]\n'
    )

    up = subprocess.run(
        ["docker", "compose", "-p", project_name, "-f", str(compose_file), "up", "-d"],
        capture_output=True, text=True,
    )
    if up.returncode != 0:
        pytest.skip(f"could not bring up a real probe container: {up.stderr}")
    try:
        holders = []
        try:
            for v in DEFAULT_PORTS:
                holders.append(held_port(int(before_ports[v])))
            for h in holders:
                h.__enter__()

            rerendered = _render(worktree, env)
            assert rerendered.returncode == 0, rerendered.stderr
        finally:
            for h in reversed(holders):
                h.__exit__(None, None, None)

        after = _read_env(worktree / "bundle" / ".env")
        for v in DEFAULT_PORTS:
            assert after[v] == before_ports[v], (
                f"{v} moved from {before_ports[v]} to {after[v]} on a re-render while a "
                f"REAL container ran under this checkout's own compose project "
                f"({project_name!r}) and its ports were externally held — "
                "own_project_running()'s real `docker compose ps` invocation did not "
                "correctly report the project as running, so the search ran and found "
                "its own ports externally taken"
            )
    finally:
        subprocess.run(
            ["docker", "compose", "-p", project_name, "-f", str(compose_file),
             "down", "-t", "1"],
            capture_output=True, text=True,
        )
