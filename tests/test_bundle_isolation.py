"""Per-checkout compose isolation: the primary bundle must keep its identity.

enterpriseaiframework-0e3 gives a linked git worktree its own compose project name and its
own port block, so a dispatched agent's `docker compose up/down` cannot operate on the
primary checkout's containers.

The half that is easy to get wrong is the PRIMARY checkout, not the worktree. Detection
compares `git rev-parse --git-dir` against `--git-common-dir`, and git reports those
inconsistently depending on the working directory: from a subdirectory (render-env.sh
cd's into bundle/) --git-dir comes back absolute while --git-common-dir comes back
relative, so a raw string comparison says "different" for the primary checkout too. The
first version shipped that way. Every worktree test still passed — a worktree's two paths
differ under either comparison — and the regression only appeared when `make up` was run
again in the primary checkout, which renamed the running bundle's compose project out
from under it and started a second, duplicate stack on offset ports.

So these run the real script in a throwaway git repo and assert both cases, and they run
it from the same relative position the Makefile does. No docker: this is about what gets
rendered into .env, which is decided before anything starts.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BUNDLE = REPO / "bundle"
RENDER = BUNDLE / "bin" / "render-env.sh"

# Defaults in docker-compose.yml. A primary checkout must keep every one of them.
DEFAULT_PORTS = {
    "GATEWAY_PORT": 4000,
    "CONTROL_PLANE_PORT": 8081,
    "IDP_PORT": 8082,
    "IDP_HTTPS_PORT": 8443,
    "FAKEPROVIDER_PORT": 8090,
    "CHAT_PORT": 3080,
}


# Every template render-env.sh renders, as (source-relative-to-bundle, ...). These tests
# run the REAL script against a minimal skeleton rather than a full bundle copy, so the
# script's inputs have to be listed somewhere — and listing them once, here, is the point:
# each of the four skeletons below used to inline its own copy calls, so adding
# searxng/settings.template.yml (enterpriseaiframework-0be) turned all five call sites red
# at once with "No such file or directory" from a redirect deep inside the script. The
# failure was correct — the script really does need the file — but the diagnosis cost is
# paid five times. A new template now needs one line here.
RENDER_INPUTS = (
    ".env.example",
    "keycloak/realm-export.template.json",
    "searxng/settings.template.yml",
)


def _skeleton(root: Path) -> Path:
    """Lay down the minimum bundle render-env.sh needs, under `root`, and return it."""
    bundle = root / "bundle"
    (bundle / "bin").mkdir(parents=True)
    shutil.copy2(RENDER, bundle / "bin" / "render-env.sh")
    for relative in RENDER_INPUTS:
        destination = bundle / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(BUNDLE / relative, destination)
    return root


def _git(*args, cwd):
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True,
    )


def _read_env(env_path: Path) -> dict:
    out = {}
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    """Run the real render-env.sh in a throwaway repo, in a primary checkout and a worktree."""
    if shutil.which("openssl") is None:
        pytest.skip("openssl not available; render-env.sh cannot generate secrets")

    root = tmp_path_factory.mktemp("isolation")
    primary = _skeleton(root / "primary")

    _git("init", "-q", cwd=primary)
    _git("config", "user.email", "test@example.com", cwd=primary)
    _git("config", "user.name", "test", cwd=primary)
    _git("add", "-A", cwd=primary)
    _git("commit", "-qm", "bundle", cwd=primary)

    worktree = root / "wt-alpha"
    _git("worktree", "add", "-q", str(worktree), "-b", "wt-alpha", cwd=primary)

    env = {**os.environ}
    # The script must not depend on inherited port/project values.
    for k in [*DEFAULT_PORTS, "COMPOSE_PROJECT_NAME"]:
        env.pop(k, None)

    results = {}
    for name, path in (("primary", primary), ("worktree", worktree)):
        # Invoked by path from the repo root, exactly as the Makefile does
        # ($(BUNDLE)/bin/render-env.sh). The script cd's into bundle/ itself — which is
        # precisely the condition that broke primary-checkout detection.
        proc = subprocess.run(
            ["bundle/bin/render-env.sh"],
            cwd=path, capture_output=True, text=True, env=env,
        )
        assert proc.returncode == 0, f"{name}: render-env.sh failed: {proc.stderr}"
        results[name] = _read_env(path / "bundle" / ".env")
    return results


def test_primary_checkout_keeps_the_canonical_project_name(rendered):
    """The regression: the primary checkout was misdetected as a worktree.

    Before the fix this returned a hashed name like `enterprise-ai-11908651`, which on the
    next `make up` started a duplicate stack instead of reusing the running one.
    """
    assert rendered["primary"]["COMPOSE_PROJECT_NAME"] == "enterprise-ai", (
        "the primary checkout was treated as a linked worktree — `make up` there will "
        "abandon the running bundle and start a second, duplicate stack"
    )


def test_primary_checkout_keeps_the_default_ports(rendered):
    """Published ports are part of the bundle's contract: docs, and anything already bound."""
    for var, default in DEFAULT_PORTS.items():
        value = rendered["primary"].get(var)
        if value is None:
            continue  # not pinned in .env; compose supplies the default
        assert int(value) == default, (
            f"primary checkout moved {var} to {value} (expected {default}) — a worktree "
            "offset was applied to the main bundle"
        )


def test_worktree_gets_its_own_project_name(rendered):
    name = rendered["worktree"]["COMPOSE_PROJECT_NAME"]
    assert name != "enterprise-ai", (
        "a linked worktree shares the primary checkout's compose project — its "
        "`docker compose down` would tear down the running bundle"
    )
    assert name.startswith("enterprise-ai-"), f"unexpected project name {name!r}"


def test_worktree_ports_do_not_collide_with_the_primary(rendered):
    """Naming alone is not enough: two bundles cannot bind the same host ports."""
    primary_ports = {
        int(rendered["primary"].get(v, d)) for v, d in DEFAULT_PORTS.items()
    }
    worktree_ports = set()
    for var, default in DEFAULT_PORTS.items():
        value = rendered["worktree"].get(var)
        assert value is not None, (
            f"worktree .env does not pin {var}, so it falls back to the primary's "
            f"default {default} and the two bundles collide on it"
        )
        worktree_ports.add(int(value))

    collisions = primary_ports & worktree_ports
    assert not collisions, f"worktree reuses primary host ports: {sorted(collisions)}"
    assert len(worktree_ports) == len(DEFAULT_PORTS), (
        "worktree derived a duplicate port for two different services"
    )


def test_worktree_ports_stay_clear_of_the_k3s_nodeport_range(rendered):
    """This host also runs k3s; NodePorts start at 30000."""
    for var in DEFAULT_PORTS:
        value = int(rendered["worktree"][var])
        assert 1024 < value < 30000, (
            f"worktree {var}={value} is outside the usable range / collides with k3s NodePorts"
        )


def test_a_primary_env_damaged_by_the_old_detection_repairs_itself(tmp_path):
    """A .env rendered while detection was broken must not keep the wrong identity.

    ensure() only fills empty values, so the derived project name and offset ports written
    by the buggy version would survive every subsequent `make up` — the primary bundle
    would go on coming up under the wrong identity on the wrong ports indefinitely. Fails
    before the self-heal branch: the stale enterprise-ai-11908651 name is left in place.
    """
    if shutil.which("openssl") is None:
        pytest.skip("openssl not available")

    repo = tmp_path / "repo"
    _skeleton(repo)
    _git("init", "-q", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "test", cwd=repo)
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "bundle", cwd=repo)

    # Exactly what the broken version left behind on this machine.
    damaged = (repo / "bundle" / ".env")
    damaged.write_text(
        "COMPOSE_PROJECT_NAME=enterprise-ai-11908651\n"
        "GATEWAY_PORT=23600\nCONTROL_PLANE_PORT=27681\nIDP_PORT=27682\n"
        "IDP_HTTPS_PORT=28043\nFAKEPROVIDER_PORT=27690\nCHAT_PORT=22680\n"
    )

    env = {**os.environ}
    for k in [*DEFAULT_PORTS, "COMPOSE_PROJECT_NAME"]:
        env.pop(k, None)
    proc = subprocess.run(
        ["bundle/bin/render-env.sh"], cwd=repo, capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0, proc.stderr

    result = _read_env(damaged)
    assert result["COMPOSE_PROJECT_NAME"] == "enterprise-ai", (
        "a primary .env damaged by the old detection kept its worktree-derived project "
        "name — the main bundle stays orphaned from its own containers"
    )
    for var, default in DEFAULT_PORTS.items():
        assert int(result[var]) == default, f"{var} was not reset to {default}"


def test_self_heal_leaves_a_deliberate_custom_project_name_alone(tmp_path):
    """Only the derived shape is reset. An operator's own name is not ours to overwrite."""
    if shutil.which("openssl") is None:
        pytest.skip("openssl not available")

    repo = tmp_path / "repo"
    _skeleton(repo)
    _git("init", "-q", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "test", cwd=repo)
    _git("add", "-A", cwd=repo)
    _git("commit", "-qm", "bundle", cwd=repo)

    (repo / "bundle" / ".env").write_text(
        "COMPOSE_PROJECT_NAME=acme-internal-ai\nGATEWAY_PORT=4000\n"
    )
    env = {**os.environ}
    for k in [*DEFAULT_PORTS, "COMPOSE_PROJECT_NAME"]:
        env.pop(k, None)
    proc = subprocess.run(
        ["bundle/bin/render-env.sh"], cwd=repo, capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert _read_env(repo / "bundle" / ".env")["COMPOSE_PROJECT_NAME"] == "acme-internal-ai"


def test_a_non_git_copy_does_not_claim_the_primary_compose_project(tmp_path):
    """The bare name `enterprise-ai` belongs to the primary checkout and nothing else.

    Detection used to ask "is this a linked worktree?" and let everything else fall through
    to the primary name. A plain copy — an extracted tarball, an agent's scratchpad checkout
    — has no .git at all, so `git rev-parse --git-dir` returns nothing, the condition was
    false, and it took `enterprise-ai`. `docker compose up` there then RECREATED the
    primary's containers with the copy's own bind-mount paths; when that directory was
    cleaned up the mounts pointed at nothing and identity crash-looped on a missing
    /certs/identity.crt. Observed on this machine, not hypothesised.

    Absence of evidence must resolve to isolation: a needlessly isolated bundle costs a few
    ports, a needlessly shared one destroys somebody's running stack.
    """
    if shutil.which("openssl") is None:
        pytest.skip("openssl not available")

    # Deliberately NOT a git repo: no `git init` anywhere above this directory.
    copy = tmp_path / "scratch-copy"
    _skeleton(copy)

    env = {**os.environ}
    for k in [*DEFAULT_PORTS, "COMPOSE_PROJECT_NAME"]:
        env.pop(k, None)
    proc = subprocess.run(
        ["bundle/bin/render-env.sh"], cwd=copy, capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0, proc.stderr

    result = _read_env(copy / "bundle" / ".env")
    assert result["COMPOSE_PROJECT_NAME"] != "enterprise-ai", (
        "a non-git copy claimed the primary checkout's compose project — `docker compose "
        "up` there would recreate the primary's containers with this directory's paths"
    )
    assert result["COMPOSE_PROJECT_NAME"].startswith("enterprise-ai-")
    for var, default in DEFAULT_PORTS.items():
        assert int(result[var]) != default, (
            f"{var} kept the primary's default {default}; the copy would collide on it"
        )
