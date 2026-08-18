"""The workspace ships a self-contained Docker daemon (and qemu) that runs when — and only
when — the pod opts in under the Sysbox runtime. See enterpriseaiframework-ee4.

Static, like tests/test_workspace_engine_support.py: it reads the image definition and the
entrypoint as the source of truth, because what they encode is a security invariant, not a
preference — the daemon must be inert in the default hardened pod, and the interactive
shell must never run as root even when a daemon is present.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
WS = REPO / "deploy" / "workspace"
DOCKERFILE = (WS / "Dockerfile").read_text()
ENTRYPOINT = (WS / "entrypoint.sh").read_text()

DOCKER_TOOLCHAIN = ["docker.io", "docker-buildx", "qemu-system-x86", "qemu-utils", "gosu"]


@pytest.mark.parametrize("pkg", DOCKER_TOOLCHAIN)
def test_image_installs_the_docker_and_qemu_toolchain(pkg):
    assert re.search(rf"(?<![\w.-]){re.escape(pkg)}(?![\w.-])", DOCKERFILE), (
        f"Dockerfile must apt-install {pkg} for the self-contained docker/qemu path"
    )


def test_buildkit_present_for_local_export():
    # OVMX builds with `docker build -o dist`, which needs BuildKit (the docker-buildx
    # plugin). Without it, the forcing use case does not work.
    assert "docker-buildx" in DOCKERFILE


def test_toolchain_is_debian_main_only():
    # The image header promises no paid-tier component and "the rest is Debian main". The
    # docker/qemu path must not add a third-party apt repo or curl vendor binaries.
    assert "download.docker.com" not in DOCKERFILE
    assert "get.docker.com" not in DOCKERFILE


def test_dockerd_starts_only_when_opted_in_and_root():
    # The daemon is inert unless WS_DOCKER=1 AND we started as container-root (which only
    # happens under Sysbox, where that root is userns-mapped to an unprivileged host UID).
    assert re.search(
        r'if \[\[ "\$\{WS_DOCKER:-\}" == "1" && "\$\(id -u\)" == "0"', ENTRYPOINT
    ), "dockerd startup must be gated on WS_DOCKER=1 && running as root"


def test_shell_never_runs_as_root_even_with_docker():
    # After starting dockerd as root we MUST drop to the unprivileged user before serving
    # the shell, and guard the re-exec so dockerd is not started twice.
    assert re.search(r'exec gosu "\$\{WS_USER\}" "\$0"', ENTRYPOINT), (
        "entrypoint must re-exec as the unprivileged user via gosu after starting dockerd"
    )
    assert "WS_DROPPED" in ENTRYPOINT, "the re-exec must be guarded against double-start"


def test_default_pod_behaviour_unchanged():
    # With WS_DOCKER unset the entire block is skipped and the entrypoint reaches its ttyd
    # exec exactly as before — the guard REQUIRES WS_DOCKER=1, so nothing about the default
    # hardened pod changes.
    assert '"${WS_DOCKER:-}" == "1"' in ENTRYPOINT
    # dockerd's output must never land in the user's terminal.
    assert ">/var/log/dockerd.log 2>&1" in ENTRYPOINT
