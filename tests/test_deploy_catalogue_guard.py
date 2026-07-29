"""enterpriseaiframework-dc0: deploy.sh must refuse to push a fakes-only catalogue over
a cluster that is currently Forge-backed.

bundle/litellm/config.generated.yaml is gitignored local state rendered by
bundle/bin/render-gateway-config.py. Whether it holds three hardcoded fakes or 148 real,
priced Forge models depends only on whether FORGE_API_KEY was visible in the ambient
environment when it was last rendered — nothing in `make up` or `deploy.sh` signals which
one just happened. deploy.sh ships that file straight into the cluster's `gateway-config`
ConfigMap, so whichever render happened last on whoever's machine wins for the live
cluster too. A deploy from a shell without Forge creds loaded silently reduces a
Forge-backed dogfood cluster to three fake models, with no error at deploy time.

deploy/bin/lib/catalogue-guard.sh's `assert_catalogue_not_downgraded` is the fix: read the
cluster's CURRENT ConfigMap (read-only `kubectl get`, never a mutation) and refuse the
deploy when the cluster is presently Forge-backed and the local file about to be pushed is
not.

No real cluster is touched by any test in this file. `kubectl` and `docker` are replaced
on PATH with stub scripts that log every invocation to a file and (unless configured to
simulate a specific cluster response) simply succeed — deploy.sh cannot tell the
difference between the stub and a real, reachable k3s cluster, and none of these tests can
mutate anything real even if the guard had a bug, because there is nothing real behind the
stub to mutate.

Two layers, per the class of drift this bug belongs to (a presence check is not a
correctness check — README/CLAUDE.md "signature defect" note):

  - `test_assert_catalogue_not_downgraded_*`: call the real bash function directly
    (sourced from the real file, not reimplemented) for every branch: refuse, allow
    same-catalogue, allow an upgrade, allow a first deploy (no ConfigMap yet), the
    explicit override, and fail CLOSED (not open) on an unrelated kubectl error. This is
    the exhaustive one — one assertion per branch of the actual refusal logic.
  - `test_deploy_sh_*`: run the real, unmodified `deploy/bin/deploy.sh` end-to-end against
    a throwaway fixture tree, to prove the guard is actually wired in — that a function
    existing but never called would not be caught by the first layer alone. One run
    proves the dangerous case is refused before any other kubectl/docker call; a second
    proves the guard does not block a legitimate deploy (the unchanged path).
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
GUARD_LIB = REPO / "deploy" / "bin" / "lib" / "catalogue-guard.sh"
DEPLOY_SH = REPO / "deploy" / "bin" / "deploy.sh"

FORGE_LINE = "      api_base: https://forge.3dl.dev/v1\n"  # what a real render contains
FAKES_ONLY_YAML = "model_list:\n  - model_name: fake-large\n    litellm_params:\n      model: openai/fake-large\n      api_base: http://fakeprovider:8080/v1\n"
FORGE_YAML = FAKES_ONLY_YAML + "  - model_name: glm-5.2\n    litellm_params:\n" + FORGE_LINE


# --------------------------------------------------------------------------------------
# Stub kubectl / docker
# --------------------------------------------------------------------------------------

_KUBECTL_STUB = """#!/usr/bin/env bash
# Test stub. Logs every invocation, then answers `get configmap ... config.yaml` from
# fixture files controlled by env vars; everything else just succeeds so deploy.sh's
# happy path can run to completion against nothing real.
echo "$*" >> "$STUB_CALLS_FILE"

if [[ "$1" == "-n" && "$3" == "get" && "$4" == "configmap" ]]; then
    case "$STUB_CM_MODE" in
        missing)
            echo 'Error from server (NotFound): configmaps "'"$5"'" not found' >&2
            exit 1
            ;;
        connerror)
            echo 'Unable to connect to the server: dial tcp 10.0.0.1:6443: i/o timeout' >&2
            exit 1
            ;;
        present)
            cat "$STUB_CM_CONTENT_FILE"
            exit 0
            ;;
        *)
            echo "test stub misconfigured: STUB_CM_MODE=$STUB_CM_MODE" >&2
            exit 99
            ;;
    esac
fi

# rollout status / get pods / create secret|configmap / apply -f - : just succeed.
exit 0
"""

_DOCKER_STUB = """#!/usr/bin/env bash
echo "$*" >> "$STUB_CALLS_FILE"
exit 0
"""


def _make_stub(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture()
def stub_bin(tmp_path) -> Path:
    d = tmp_path / "stubbin"
    d.mkdir()
    _make_stub(d / "kubectl", _KUBECTL_STUB)
    _make_stub(d / "docker", _DOCKER_STUB)
    return d


# --------------------------------------------------------------------------------------
# Layer 1 — the function itself, every branch
# --------------------------------------------------------------------------------------


def _run_guard(tmp_path, stub_bin, calls_file, cm_mode, cm_content, local_content, extra_env=None):
    """Source the real catalogue-guard.sh and call assert_catalogue_not_downgraded."""
    local_cfg = tmp_path / "config.generated.yaml"
    if local_content is None:
        assert not local_cfg.exists()
    else:
        local_cfg.write_text(local_content)

    cm_content_file = tmp_path / "cluster_cm.yaml"
    cm_content_file.write_text(cm_content or "")

    env = {
        **os.environ,
        "PATH": f"{stub_bin}:{os.environ['PATH']}",
        "STUB_CALLS_FILE": str(calls_file),
        "STUB_CM_MODE": cm_mode,
        "STUB_CM_CONTENT_FILE": str(cm_content_file),
    }
    for k in ("FORGE_API_KEY", "FORGE_ADMIN_KEY"):
        env.pop(k, None)
    if extra_env:
        env.update(extra_env)

    script = (
        f"set -euo pipefail\n"
        f"source '{GUARD_LIB}'\n"
        f"assert_catalogue_not_downgraded enterprise-ai gateway-config '{local_cfg}'\n"
    )
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, env=env, timeout=30,
    )


def test_refuses_when_cluster_is_forge_backed_and_local_is_fakes_only(tmp_path, stub_bin):
    calls = tmp_path / "calls.log"
    calls.touch()
    proc = _run_guard(
        tmp_path, stub_bin, calls,
        cm_mode="present", cm_content=FORGE_YAML, local_content=FAKES_ONLY_YAML,
    )
    assert proc.returncode == 1, proc.stderr
    assert "refusing to deploy" in proc.stderr
    assert "downgrade" in proc.stderr.lower() or "fakes-only" in proc.stderr.lower()
    # The guard itself must not have called anything beyond the read it needed.
    logged = calls.read_text().splitlines()
    assert logged == [
        r"-n enterprise-ai get configmap gateway-config -o jsonpath={.data.config\.yaml}"
    ], logged


def test_allows_when_cluster_and_local_are_both_forge_backed(tmp_path, stub_bin):
    """The unchanged path: redeploying an already-Forge-backed cluster with a fresh
    Forge-backed render (e.g. an updated price list) must not be blocked."""
    calls = tmp_path / "calls.log"
    calls.touch()
    proc = _run_guard(
        tmp_path, stub_bin, calls,
        cm_mode="present", cm_content=FORGE_YAML, local_content=FORGE_YAML,
    )
    assert proc.returncode == 0, proc.stderr


def test_allows_when_cluster_and_local_are_both_fakes_only(tmp_path, stub_bin):
    """The unchanged path: a demo cluster kept fakes-only stays deployable."""
    calls = tmp_path / "calls.log"
    calls.touch()
    proc = _run_guard(
        tmp_path, stub_bin, calls,
        cm_mode="present", cm_content=FAKES_ONLY_YAML, local_content=FAKES_ONLY_YAML,
    )
    assert proc.returncode == 0, proc.stderr


def test_allows_upgrading_a_fakes_only_cluster_to_forge(tmp_path, stub_bin):
    """The opposite direction (fakes -> real) is not a downgrade and must be allowed."""
    calls = tmp_path / "calls.log"
    calls.touch()
    proc = _run_guard(
        tmp_path, stub_bin, calls,
        cm_mode="present", cm_content=FAKES_ONLY_YAML, local_content=FORGE_YAML,
    )
    assert proc.returncode == 0, proc.stderr


def test_allows_first_deploy_when_configmap_does_not_exist_yet(tmp_path, stub_bin):
    calls = tmp_path / "calls.log"
    calls.touch()
    proc = _run_guard(
        tmp_path, stub_bin, calls,
        cm_mode="missing", cm_content="", local_content=FAKES_ONLY_YAML,
    )
    assert proc.returncode == 0, proc.stderr


def test_override_bypasses_the_refusal(tmp_path, stub_bin):
    calls = tmp_path / "calls.log"
    calls.touch()
    proc = _run_guard(
        tmp_path, stub_bin, calls,
        cm_mode="present", cm_content=FORGE_YAML, local_content=FAKES_ONLY_YAML,
        extra_env={"DEPLOY_CONFIRM_CATALOGUE_DOWNGRADE": "1"},
    )
    assert proc.returncode == 0, proc.stderr
    assert "skipped" in proc.stderr.lower()


def test_fails_closed_not_open_on_an_unrelated_kubectl_error(tmp_path, stub_bin):
    """A guard that treats every kubectl error as 'no ConfigMap yet' is not a guard — a
    transient network blip would silently wave a real downgrade through. Only the
    NotFound case (genuinely no ConfigMap) is allowed to pass; anything else refuses."""
    calls = tmp_path / "calls.log"
    calls.touch()
    proc = _run_guard(
        tmp_path, stub_bin, calls,
        cm_mode="connerror", cm_content="", local_content=FAKES_ONLY_YAML,
    )
    assert proc.returncode == 1, proc.stderr
    assert "could not read" in proc.stderr.lower()


# --------------------------------------------------------------------------------------
# Layer 2 — deploy.sh actually wires the guard in, before any other kubectl/docker call
# --------------------------------------------------------------------------------------


@pytest.fixture()
def deploy_fixture(tmp_path_factory):
    """A throwaway copy of just enough of the repo for the real deploy.sh to run,
    against stubbed kubectl/docker, all the way to completion when allowed."""
    root = tmp_path_factory.mktemp("deploy-fixture")

    (root / "deploy" / "bin" / "lib").mkdir(parents=True)
    (root / "deploy" / "k8s").mkdir(parents=True)
    (root / "deploy" / "gateway").mkdir(parents=True)
    (root / "bundle" / "litellm").mkdir(parents=True)
    (root / "bundle" / "librechat").mkdir(parents=True)

    shutil.copy2(DEPLOY_SH, root / "deploy" / "bin" / "deploy.sh")
    (root / "deploy" / "bin" / "deploy.sh").chmod(0o755)
    shutil.copy2(GUARD_LIB, root / "deploy" / "bin" / "lib" / "catalogue-guard.sh")

    for f in ("00-namespace", "10-postgres", "11-data", "20-identity", "30-gateway",
              "50-chat", "40-control-plane"):
        (root / "deploy" / "k8s" / f"{f}.yaml").write_text(f"# placeholder {f}\n")
    (root / "deploy" / "gateway" / "strip_reasoning.py").write_text("# placeholder\n")
    (root / "bundle" / "librechat" / "librechat.yaml").write_text(
        "endpoints:\n  custom:\n    - baseURL: http://gateway:4000/v1\n"
    )

    env_vars = {
        "POSTGRES_USER": "eai", "POSTGRES_PASSWORD": "x", "GATEWAY_MASTER_KEY": "x",
        "GATEWAY_SALT_KEY": "x", "CONTROL_PLANE_ADMIN_TOKEN": "x",
        "IDP_ADMIN_USER": "admin", "IDP_ADMIN_PASSWORD": "x", "IDP_CLIENT_SECRET": "x",
        "CHAT_CLIENT_SECRET": "x", "CHAT_SESSION_SECRET": "x", "CHAT_JWT_SECRET": "x",
        "CHAT_JWT_REFRESH_SECRET": "x", "CHAT_CREDS_KEY": "x", "CHAT_CREDS_IV": "x",
        "CHAT_VIRTUAL_KEY": "", "IDP_REALM": "enterprise-ai", "FORGE_API_KEY": "",
    }
    (root / "bundle" / ".env").write_text(
        "\n".join(f"{k}={v}" for k, v in env_vars.items()) + "\n"
    )
    return root


def _run_deploy_sh(root, stub_bin, calls_file, cm_mode, cm_content, local_content):
    (root / "bundle" / "litellm" / "config.generated.yaml").write_text(local_content)
    cm_content_file = root / "cluster_cm.yaml"
    cm_content_file.write_text(cm_content or "")

    env = {
        **os.environ,
        "PATH": f"{stub_bin}:{os.environ['PATH']}",
        "PUBLIC_BASE_URL": "https://ai.example.test",
        "STUB_CALLS_FILE": str(calls_file),
        "STUB_CM_MODE": cm_mode,
        "STUB_CM_CONTENT_FILE": str(cm_content_file),
    }
    for k in ("FORGE_API_KEY", "FORGE_ADMIN_KEY"):
        env.pop(k, None)

    return subprocess.run(
        ["deploy/bin/deploy.sh"], cwd=root, capture_output=True, text=True, env=env,
        timeout=60,
    )


def test_deploy_sh_refuses_before_any_other_kubectl_or_docker_call(deploy_fixture, stub_bin):
    calls = deploy_fixture / "calls.log"
    calls.touch()
    proc = _run_deploy_sh(
        deploy_fixture, stub_bin, calls,
        cm_mode="present", cm_content=FORGE_YAML, local_content=FAKES_ONLY_YAML,
    )
    assert proc.returncode == 1, (proc.stdout, proc.stderr)
    assert "refusing to deploy" in proc.stderr

    logged = calls.read_text().splitlines()
    # The only permitted call is the guard's own read of the current ConfigMap. No
    # namespace apply, no secret, no configmap write, no docker build/push.
    assert logged == [
        r"-n enterprise-ai get configmap gateway-config -o jsonpath={.data.config\.yaml}"
    ], f"deploy.sh made calls beyond the guard's own read before refusing: {logged}"


def test_deploy_sh_still_completes_a_legitimate_deploy(deploy_fixture, stub_bin):
    """The unchanged path: cluster and local catalogue agree (both fakes-only, as a
    freshly-`make up`'d compose bundle and a freshly-installed demo cluster would), so
    the guard must not be in the way of a real deploy completing."""
    calls = deploy_fixture / "calls.log"
    calls.touch()
    proc = _run_deploy_sh(
        deploy_fixture, stub_bin, calls,
        cm_mode="present", cm_content=FAKES_ONLY_YAML, local_content=FAKES_ONLY_YAML,
    )
    assert proc.returncode == 0, (proc.stdout, proc.stderr)

    logged = calls.read_text().splitlines()
    assert any("apply -f deploy/k8s/00-namespace.yaml" in line for line in logged), logged
    assert any("create secret" in line for line in logged), logged
    assert any("gateway-config" in line and "create configmap" in line for line in logged), logged
