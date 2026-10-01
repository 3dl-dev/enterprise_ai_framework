"""A manifest deploy.sh holds back must not carry workloads it could have applied.

deploy.sh applies or holds back a manifest FILE as a unit. A file with an
`image: REPLACED_BY_DEPLOY` it does not build (everything except
40-control-plane.yaml) is held back whole. On 2026-10-01 that silently held back
codeapi-minio's image pin, because minio and redis (third-party images that
need no build) shared 70-codeapi.yaml with codeapi's unbuilt images. The
rollout still waited on minio, so every deploy failed.

So in a held-back file, every container image must be a build placeholder.
"""
import pathlib
import re

import yaml

K8S = pathlib.Path(__file__).resolve().parent.parent / "deploy" / "k8s"
BUILT_BY_DEPLOY = {"40-control-plane.yaml"}  # deploy.sh substitutes this one's image itself
PLACEHOLDER = re.compile(r"^(REPLACED_BY_DEPLOY|__[A-Z_]+__)$")


def _containers(doc):
    spec = (doc or {}).get("spec") or {}
    tmpl = spec.get("template") or spec.get("jobTemplate", {}).get("spec", {}).get("template") or {}
    pod = tmpl.get("spec") or {}
    return list(pod.get("initContainers") or []) + list(pod.get("containers") or [])


def held_back_violations(files):
    bad = []
    for path in files:
        if path.name in BUILT_BY_DEPLOY or ".template." in path.name:
            continue
        docs = list(yaml.safe_load_all(path.read_text()))
        images = [(d["kind"], d["metadata"]["name"], c.get("image", "")) for d in docs if d for c in _containers(d)]
        if not any(img == "REPLACED_BY_DEPLOY" for _, _, img in images):
            continue  # applied, not held back
        bad += [f"{path.name}: {k}/{n} uses {img}" for k, n, img in images if not PLACEHOLDER.match(img)]
    return bad


def test_held_back_manifests_contain_only_build_placeholder_workloads():
    bad = held_back_violations(sorted(K8S.glob("*.yaml")))
    assert not bad, "these workloads would be held back with their file and never applied:\n" + "\n".join(bad)


def test_the_check_sees_codeapi_as_held_back(tmp_path):
    # positive control: a held-back file that also carries a registry image is flagged
    f = tmp_path / "99-mixed.yaml"
    f.write_text(
        "apiVersion: apps/v1\nkind: Deployment\nmetadata: {name: built}\nspec:\n  template:\n    spec:\n"
        "      containers: [{name: a, image: REPLACED_BY_DEPLOY}]\n---\n"
        "apiVersion: apps/v1\nkind: Deployment\nmetadata: {name: thirdparty}\nspec:\n  template:\n    spec:\n"
        "      containers: [{name: b, image: 'quay.io/x/y:1.0'}]\n")
    assert held_back_violations([f]) == ["99-mixed.yaml: Deployment/thirdparty uses quay.io/x/y:1.0"]
