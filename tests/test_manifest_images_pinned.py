"""Every image a shipped manifest pulls from a registry must be pinned.

An untagged image or `:latest` means kubelet re-pulls on every container restart.
When the upstream tag stops resolving, the pod crash-loops in ImagePullBackOff
and a deploy's rollout wait hangs on it. That happened to codeapi-minio
(2026-10-01). A version tag or a digest makes a restart reuse a known image.

Placeholders that deploy.sh or the provisioners substitute (REPLACED_BY_DEPLOY,
__IMAGE__, __VOICE_WORKER_IMAGE__) are built and tagged by this repo, so they're
exempt here.
"""
import pathlib
import re

import pytest

K8S = pathlib.Path(__file__).resolve().parent.parent / "deploy" / "k8s"
IMAGE_LINE = re.compile(r"^\s*-?\s*image:\s*([^\s#]+)", re.M)
PLACEHOLDER = re.compile(r"^(REPLACED_BY_DEPLOY|__[A-Z_]+__)$")


def unpinned(ref: str) -> bool:
    if PLACEHOLDER.match(ref):
        return False
    if "@sha256:" in ref:
        return False
    last = ref.rsplit("/", 1)[-1]
    if ":" not in last:
        return True  # no tag at all == :latest
    return last.rsplit(":", 1)[1] == "latest"


def shipped_images():
    for path in sorted(K8S.glob("*.yaml")):
        for ref in IMAGE_LINE.findall(path.read_text()):
            yield path.name, ref


def test_the_scan_finds_images():
    assert len(list(shipped_images())) > 20


def test_every_registry_image_is_pinned_to_a_tag_or_digest():
    bad = [f"{name}: {ref}" for name, ref in shipped_images() if unpinned(ref)]
    assert not bad, "unpinned images (no tag or :latest) re-pull on every restart:\n" + "\n".join(bad)


@pytest.mark.parametrize("ref, expected", [
    ("quay.io/minio/minio", True),
    ("searxng/searxng:latest", True),
    ("registry:5000/team/app", True),
    ("registry:5000/team/app:latest", True),
    ("postgres:16-alpine", False),
    ("quay.io/minio/minio@sha256:" + "a" * 64, False),
    ("registry:5000/team/app:1.2", False),
    ("REPLACED_BY_DEPLOY", False),
    ("__VOICE_WORKER_IMAGE__", False),
])
def test_unpinned_classifier(ref, expected):
    assert unpinned(ref) is expected
