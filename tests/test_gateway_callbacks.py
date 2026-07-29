"""Every callback module the gateway config names must actually reach the gateway.

litellm loads `litellm_settings.callbacks` by bare module name from the proxy's working
directory. If the config names a module the container does not have, the proxy raises
ImportError during startup and exits — the gateway crash-loops and every surface is down.

This is a drift trap rather than an ordinary bug. The config is ONE artifact
(config.generated.yaml) mounted by TWO deployments, so naming a callback in it places an
obligation on both; satisfying that obligation in one deployment, or by hand-copying the
file into a running pod, leaves the other broken while the healthy one reads as proof
that the configuration is right. That is what happened: the compose gateway crash-looped
on `strip_reasoning.handler` while the k3s pod ran green on a hand-placed copy that no
manifest declared and that would have vanished on its next restart.

So the invariant is checked three times, once per place the obligation can be dropped:
against the running container (ground truth), against the compose file, and against the
cluster manifests. A presence check on one deployment is not a correctness check on the
configuration.
"""

import re
import subprocess
from pathlib import Path

import pytest

from conftest import BUNDLE, compose

REPO = Path(__file__).resolve().parent.parent
GENERATED_CONFIG = BUNDLE / "litellm" / "config.generated.yaml"
COMPOSE_FILE = BUNDLE / "docker-compose.yml"
K8S_GATEWAY = REPO / "deploy" / "k8s" / "30-gateway.yaml"
DEPLOY_SH = REPO / "deploy" / "bin" / "deploy.sh"


def _callback_modules() -> list[str]:
    """Module names in `litellm_settings.callbacks`, e.g. 'strip_reasoning.handler' -> 'strip_reasoning'.

    Parsed with a regex rather than a YAML loader to avoid adding a dependency to the
    test venv for one list. The generated config is machine-written, so the form is stable.
    """
    if not GENERATED_CONFIG.exists():
        pytest.exit(f"{GENERATED_CONFIG} missing — run `make up` first", returncode=2)
    text = GENERATED_CONFIG.read_text()
    match = re.search(r"^\s*callbacks:\s*\[(?P<body>[^\]]*)\]", text, re.MULTILINE)
    if not match:
        return []
    entries = re.findall(r"['\"]([^'\"]+)['\"]", match.group("body"))
    # "module.attr" -> "module". A bare "module" is also legal.
    return sorted({e.split(".")[0] for e in entries if e.strip()})


CALLBACK_MODULES = _callback_modules()

# If the config stops naming callbacks these tests have nothing to assert, and silently
# passing would hide the fact that the guard went away with it.
requires_callbacks = pytest.mark.skipif(
    not CALLBACK_MODULES,
    reason="gateway config names no callbacks",
)


def test_config_names_at_least_one_callback():
    """Guard the guard: strip_reasoning is load-bearing, so its absence is a finding."""
    assert "strip_reasoning" in CALLBACK_MODULES, (
        f"config.generated.yaml no longer loads strip_reasoning (callbacks: {CALLBACK_MODULES}). "
        "If that removal was deliberate, delete this assertion deliberately too — reasoning "
        "traces reaching the surfaces is the behaviour it was added to stop."
    )


@requires_callbacks
@pytest.mark.parametrize("module", CALLBACK_MODULES)
def test_callback_module_is_present_in_the_running_gateway(stack_up, module):
    """Ground truth: the file is in the container that has to import it.

    Fails before the fix — /app/strip_reasoning.py did not exist and the proxy was in a
    restart loop. The container is resolved through `docker compose ps -q` rather than by
    a hardcoded name so this keeps working once the compose project name is per-worktree.
    """
    cid = compose("ps", "-q", "gateway").stdout.strip()
    assert cid, "gateway container is not running"
    probe = subprocess.run(
        ["docker", "exec", cid, "test", "-f", f"/app/{module}.py"],
        capture_output=True, text=True,
    )
    assert probe.returncode == 0, (
        f"config.generated.yaml loads callback '{module}' but /app/{module}.py does not "
        f"exist in the gateway container. litellm imports callbacks by bare module name "
        f"from the working directory, so the proxy will ImportError on startup and "
        f"crash-loop."
    )


@requires_callbacks
@pytest.mark.parametrize("module", CALLBACK_MODULES)
def test_compose_bundle_mounts_the_callback_module(module):
    """The compose gateway declares a mount landing at /app/<module>.py."""
    text = COMPOSE_FILE.read_text()
    target = f":/app/{module}.py:"
    assert target in text, (
        f"bundle/docker-compose.yml does not mount anything at /app/{module}.py, but the "
        f"gateway config loads callback '{module}'. The compose gateway will crash-loop."
    )


@requires_callbacks
@pytest.mark.parametrize("module", CALLBACK_MODULES)
def test_cluster_manifests_ship_the_callback_module(module):
    """The cluster mounts it by declared configuration, not by hand.

    Both halves matter: the manifest can mount a subPath that the configmap never
    supplies, which fails at pod start rather than at deploy time.
    """
    manifest = K8S_GATEWAY.read_text()
    assert f"subPath: {module}.py" in manifest, (
        f"deploy/k8s/30-gateway.yaml does not mount {module}.py into the gateway, but the "
        f"gateway config loads callback '{module}'. The pod will crash-loop on restart."
    )
    assert f"mountPath: /app/{module}.py" in manifest, (
        f"deploy/k8s/30-gateway.yaml mounts {module}.py somewhere other than /app/{module}.py; "
        f"litellm imports callbacks from the proxy working directory."
    )
    deploy_sh = DEPLOY_SH.read_text()
    assert f"--from-file={module}.py=" in deploy_sh, (
        f"deploy/bin/deploy.sh builds configmap gateway-config without a {module}.py key, "
        f"so the subPath mount in 30-gateway.yaml has nothing to mount and the pod will "
        f"fail to start."
    )


@requires_callbacks
@pytest.mark.parametrize("module", CALLBACK_MODULES)
def test_callback_module_change_rolls_the_cluster_deployment(module):
    """A callback edit must change checksum/config, or the cluster keeps the old code.

    Without this the module is deployable but not redeployable: `deploy.sh` updates the
    configmap and the running pod goes on serving the previous version until something
    unrelated restarts it.
    """
    source = next(REPO.glob(f"deploy/gateway/{module}.py"), None)
    assert source is not None, (
        f"gateway config loads callback '{module}' but deploy/gateway/{module}.py does not "
        f"exist — nothing can ship it."
    )
    deploy_sh = DEPLOY_SH.read_text()
    checksum_line = next(
        (ln for ln in deploy_sh.splitlines() if ln.startswith("CFG_SUM=")), ""
    )
    assert f"deploy/gateway/{module}.py" in checksum_line, (
        f"CFG_SUM in deploy/bin/deploy.sh does not cover deploy/gateway/{module}.py, so "
        f"editing the callback will not roll the gateway pod and the cluster will keep "
        f"running the old module."
    )
