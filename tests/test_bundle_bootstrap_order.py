"""`make up` in a genuinely fresh worktree left identity unhealthy.

Found running `make up` in a brand-new worktree while working
enterpriseaiframework-cbf: an untested combination, since the primary checkout and every
other worktree so far already had a bundle/.env before the ordering below could matter.

`bundle/bin/make-certs.sh` only records IDP_PUBLIC_HOST into a .env that already exists
(`elif [[ -f .env ]]`); it does nothing at all if .env is absent. `bundle/bin/render-env.sh`
is the script that CREATES .env on a first run (from .env.example). The Makefile's `up`
target used to run make-certs.sh first — so on a fresh worktree, make-certs.sh saw no
.env, did nothing, and by the time render-env.sh created one a moment later,
IDP_PUBLIC_HOST was never written. identity then started with a malformed KC_HOSTNAME
(`https:` with nothing after it) and never became healthy.

The fix is the order, not the scripts: render-env.sh before make-certs.sh. This runs both
REAL scripts, unmodified, in a scratch directory shaped like `bundle/` (so their own
`cd "$(dirname "$0")/.."` lands them in the right place) — first in the OLD order, to prove
the ordering dependency actually bites and is not merely inferred from reading the scripts,
then in the FIXED order, to prove the fix actually closes it. A test that only exercised
the fixed order would not have caught this regression if the bug returns from a future
edit; a test that only re-derives the failure from reading the scripts would not survive
Testing Supremacy's "demonstrate, don't assert."

SECOND FIX, MERGED IN LATER, and it changed what the wrong order does.
`enterpriseaiframework-d58` found the same bug independently and fixed it from the other
end: make-certs.sh now exits 1 with a message naming render-env.sh instead of silently
falling through when .env is absent. The wrong order therefore no longer produces a
malformed bundle — it refuses to proceed. `test_the_broken_order_now_fails_loudly_instead_of_silently`
was updated to assert the refusal rather than the silence; see its docstring for why that
is a strengthening rather than a weakening.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BUNDLE = REPO / "bundle"
MAKEFILE = REPO / "Makefile"


def _scratch_bundle(tmp_path: Path) -> Path:
    """A directory shaped like bundle/: bin/{make-certs,render-env}.sh + .env.example.

    Both scripts only touch .env, certs/ and a handful of `openssl rand`-generated
    secrets — nothing that reaches outside this directory or the network (other than
    `ip route get`, which reads the local routing table and needs no connectivity).
    """
    scratch = tmp_path / "bundle"
    (scratch / "bin").mkdir(parents=True)
    for name in ("make-certs.sh", "render-env.sh"):
        src = BUNDLE / "bin" / name
        dst = scratch / "bin" / name
        shutil.copy2(src, dst)
        dst.chmod(0o755)
    shutil.copy2(BUNDLE / ".env.example", scratch / ".env.example")
    # render-env.sh also renders keycloak/realm-export.json from this template — needed
    # for the script to exit 0, irrelevant to what this test is checking.
    (scratch / "keycloak").mkdir()
    shutil.copy2(
        BUNDLE / "keycloak" / "realm-export.template.json",
        scratch / "keycloak" / "realm-export.template.json",
    )
    return scratch


def _run(script: Path, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(script)], cwd=str(cwd), capture_output=True, text=True, timeout=30
    )


def _idp_public_host(env_path: Path) -> str | None:
    if not env_path.exists():
        return None
    for line in env_path.read_text().splitlines():
        if line.startswith("IDP_PUBLIC_HOST="):
            return line.split("=", 1)[1]
    return None


def test_the_broken_order_now_fails_loudly_instead_of_silently(tmp_path):
    """make-certs.sh before render-env.sh, on a worktree with no .env yet.

    UPDATED, AND THE UPDATE IS THE POINT — do not "fix" this back. This test used to
    assert that the broken order exits 0 and silently writes nothing, because that is
    exactly what it did when the bug was found (enterpriseaiframework-cbf): all three
    branches fell through in silence and nothing noticed until Keycloak died on
    "https://:8443" ten containers later.

    enterpriseaiframework-d58 then fixed the same bug from the other end, independently:
    make-certs.sh now refuses to run at all without a .env instead of skipping. Both
    changes are right and they compose — the Makefile ordering fix means the refusal is
    never reached in a correct invocation, and the refusal means that if the ordering
    regresses it stops the build with a message naming the fix, rather than producing a
    bundle that cannot start.

    So the assertion moved from "silently does nothing" to "loudly refuses", which is a
    strictly stronger guarantee about the same ordering dependency. What has NOT changed
    is what this test exists to prove: the dependency is real, and it is demonstrated by
    running the real scripts in the wrong order rather than inferred from reading them.
    """
    scratch = _scratch_bundle(tmp_path)
    assert not (scratch / ".env").exists()

    certs = _run(scratch / "bin" / "make-certs.sh", scratch)
    assert certs.returncode != 0, (
        "make-certs.sh ran without a .env and did not complain. That is the silent "
        "fall-through this bug was made of: IDP_PUBLIC_HOST goes unwritten, compose "
        "resolves KC_HOSTNAME to 'https://:8443', and identity crash-loops ten "
        f"containers later.\nstdout: {certs.stdout}\nstderr: {certs.stderr}"
    )
    assert "render-env.sh" in certs.stderr, (
        "the refusal must name the script that fixes it — an error that does not say "
        f"what to run next is how this cost an afternoon the first time: {certs.stderr}"
    )
    # It refused; it did not half-write something. .env is still absent, so render-env.sh
    # is still free to create it from scratch.
    assert not (scratch / ".env").exists()

    # And the ordering dependency is genuinely one-way: render-env.sh needs nothing from
    # make-certs.sh, so the correct order recovers with no trace of the failed attempt.
    env = _run(scratch / "bin" / "render-env.sh", scratch)
    assert env.returncode == 0, env.stderr
    assert (scratch / ".env").exists()
    certs = _run(scratch / "bin" / "make-certs.sh", scratch)
    assert certs.returncode == 0, certs.stderr
    assert _idp_public_host(scratch / ".env"), (
        "after the correct order, IDP_PUBLIC_HOST must be set — the recovery from the "
        "wrong order must be complete, not partial"
    )


def test_the_fixed_order_writes_idp_public_host_on_a_fresh_worktree(tmp_path):
    """render-env.sh before make-certs.sh: the fix, run against the real scripts."""
    scratch = _scratch_bundle(tmp_path)
    assert not (scratch / ".env").exists()

    env = _run(scratch / "bin" / "render-env.sh", scratch)
    assert env.returncode == 0, env.stderr
    assert (scratch / ".env").exists()

    certs = _run(scratch / "bin" / "make-certs.sh", scratch)
    assert certs.returncode == 0, certs.stderr

    host = _idp_public_host(scratch / ".env")
    assert host, "IDP_PUBLIC_HOST must be set after make-certs.sh runs against an existing .env"
    assert host != "", "IDP_PUBLIC_HOST must not be the empty string (a malformed KC_HOSTNAME)"


def test_a_second_run_still_updates_idp_public_host_if_the_host_ip_changed(tmp_path):
    """Not just the fresh case: make-certs.sh's `grep -qE '^IDP_PUBLIC_HOST=' .env` branch
    (an existing, non-empty value gets overwritten) must still work after the reorder —
    this is the branch that runs on every subsequent `make up`, not just the first.
    """
    scratch = _scratch_bundle(tmp_path)
    _run(scratch / "bin" / "render-env.sh", scratch)
    _run(scratch / "bin" / "make-certs.sh", scratch)
    first = _idp_public_host(scratch / ".env")
    assert first

    # Simulate the host IP having changed since the last run.
    env_path = scratch / ".env"
    lines = env_path.read_text().splitlines()
    lines = [
        "IDP_PUBLIC_HOST=203.0.113.99" if l.startswith("IDP_PUBLIC_HOST=") else l
        for l in lines
    ]
    env_path.write_text("\n".join(lines) + "\n")
    assert _idp_public_host(env_path) == "203.0.113.99"

    _run(scratch / "bin" / "make-certs.sh", scratch)
    second = _idp_public_host(env_path)
    assert second == first, (
        "a second run must overwrite the stale placeholder back to the real host IP"
    )


def test_makefile_up_target_runs_render_env_before_make_certs():
    """Pin the order directly, so a future edit that puts make-certs.sh back first fails
    immediately without needing the slower script-level reproduction above.
    """
    text = MAKEFILE.read_text()
    m = re.search(r"^up:\n((?:\t.*\n?)+)", text, re.MULTILINE)
    assert m, "no 'up:' target found in Makefile"
    body = m.group(1)

    render_pos = body.find("render-env.sh")
    certs_pos = body.find("make-certs.sh")
    assert render_pos != -1, "render-env.sh not called from the up target"
    assert certs_pos != -1, "make-certs.sh not called from the up target"
    assert render_pos < certs_pos, (
        "render-env.sh must run before make-certs.sh in the `up` target — make-certs.sh "
        "only records IDP_PUBLIC_HOST into a .env that already exists, and render-env.sh "
        "is what creates .env on a fresh worktree"
    )
