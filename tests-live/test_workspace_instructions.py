"""The terminal agent's house rules are tenant configuration (enterpriseaiframework-cbf),
tested against the live cluster with real kubectl calls and real pods — not simulated.

A prior version of this file asserted the mechanism by running
`kubectl create configmap ... --dry-run=client -o json` directly. That command executes
kubectl's own client-side generator against a file on disk; it does not call
deploy/bin/provision-workspace.sh or deploy/bin/lib/tenant-instructions.sh, so it exercised
ZERO lines of this repo's code. Every test below either (a) calls
`ensure_tenant_instructions` — the actual function `provision-workspace.sh` calls — via a
real `kubectl apply` round trip against the real API server, or (b) drives the real
`deploy/bin/provision-workspace.sh` script and inspects a real pod.

Requires:
  - cluster access (kubectl context pointed at the enterprise-ai namespace)
  - the workspace image built and pushed at the tag WORKSPACE_TEST_IMAGE names (default:
    192.168.2.43:30500/enterprise-ai-workspace:cbf-rework — built via
    `deploy/bin/kaniko-build.sh deploy/workspace <that tag>` from this branch)
  - deploy/bin/ensure-second-user.sh cbftest / cbftest2 already run once (idempotent; the
    lifecycle test below runs it itself)

Run with:  .venv-test/bin/pytest tests-live/test_workspace_instructions.py -v --tb=short
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
import yaml

NS = "enterprise-ai"
REPO = Path(__file__).resolve().parent.parent
LIB = REPO / "deploy/bin/lib/tenant-instructions.sh"
AGENTS_MD = REPO / "deploy/workspace/AGENTS.md"
PLATFORM_MD = REPO / "deploy/workspace/PLATFORM.md"
NETPOL_YAML = REPO / "deploy/k8s/60-workspace-common.yaml"
TEST_IMAGE = os.environ.get(
    "WORKSPACE_TEST_IMAGE", "192.168.2.43:30500/enterprise-ai-workspace:cbf-rework"
)
PROD_CM = "workspace-tenant-instructions"


def _kubectl(*args, check=True, timeout=120):
    return subprocess.run(
        ["kubectl", "-n", NS, *args], capture_output=True, text=True, timeout=timeout, check=check
    )


def _cm_exists(name: str) -> bool:
    return _kubectl("get", "configmap", name, check=False).returncode == 0


def _cm_tenant_md(name: str) -> str:
    return _kubectl("get", "configmap", name, "-o", "jsonpath={.data.TENANT\\.md}").stdout


def _delete_cm(name: str):
    _kubectl("delete", "configmap", name, "--ignore-not-found", check=False)


def _call_ensure(ns: str, cm: str, default_seed: str, instructions: str = "") -> subprocess.CompletedProcess:
    """Invoke the REAL function, sourced from the REAL file, via a real subshell.

    This is not a reimplementation: it is `source deploy/bin/lib/tenant-instructions.sh`
    followed by calling the function the file defines, exactly as
    deploy/bin/provision-workspace.sh does.
    """
    script = f'source "{LIB}"; ensure_tenant_instructions "{ns}" "{cm}" "{default_seed}" "{instructions}"'
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=60, cwd=str(REPO)
    )


# ============================================================== branch tests (throwaway CM)
# Each of the three branches ensure_tenant_instructions can take, run for real against a
# throwaway ConfigMap name so these never touch the deployment-wide `workspace-tenant-
# instructions` object every real workspace pod mounts.

@pytest.fixture()
def scratch_cm():
    name = f"cbf-test-lib-{uuid.uuid4().hex[:10]}"
    yield name
    _delete_cm(name)


def test_seeds_default_when_configmap_is_missing(scratch_cm):
    """Branch 1: nothing passed, nothing exists yet -> seed from AGENTS.md."""
    assert not _cm_exists(scratch_cm), "the throwaway name must not already exist"

    result = _call_ensure(NS, scratch_cm, str(AGENTS_MD))

    assert result.returncode == 0, result.stderr
    assert "seeded default" in result.stdout
    assert _cm_exists(scratch_cm)
    assert _cm_tenant_md(scratch_cm) == AGENTS_MD.read_text(), (
        "the seeded ConfigMap must equal deploy/workspace/AGENTS.md byte-for-byte "
        "(done-condition 3: the camp's current file survives verbatim)"
    )


def test_leaves_existing_configmap_untouched_when_no_instructions_given(scratch_cm):
    """Branch 2: something is already there and no --instructions given -> do not reset it.

    This is the branch that protects an operator's customisation from a second, unrelated
    `provision-workspace.sh <other-user>` call silently reverting it to the camp default.
    """
    first = _call_ensure(NS, scratch_cm, str(AGENTS_MD))
    assert first.returncode == 0, first.stderr
    assert _cm_tenant_md(scratch_cm) == AGENTS_MD.read_text()

    # Hand-edit the ConfigMap directly, the way an operator's earlier --instructions run
    # would have left it, then call again with nothing.
    _kubectl("patch", "configmap", scratch_cm, "--type=merge",
             "-p", json.dumps({"data": {"TENANT.md": "operator's own text\n"}}))
    assert _cm_tenant_md(scratch_cm) == "operator's own text\n"

    second = _call_ensure(NS, scratch_cm, str(AGENTS_MD))

    assert second.returncode == 0, second.stderr
    assert "unchanged" in second.stdout
    assert _cm_tenant_md(scratch_cm) == "operator's own text\n", (
        "a run with no --instructions must NOT reset an operator's existing configuration"
    )


def test_overwrites_when_instructions_given(scratch_cm, tmp_path):
    """Branch 3: --instructions given -> overwrite unconditionally, seeded or not."""
    _call_ensure(NS, scratch_cm, str(AGENTS_MD))
    assert _cm_tenant_md(scratch_cm) == AGENTS_MD.read_text()

    custom = tmp_path / "custom.md"
    custom.write_text("a different deployment's rules\n")

    result = _call_ensure(NS, scratch_cm, str(AGENTS_MD), str(custom))

    assert result.returncode == 0, result.stderr
    assert "updated" in result.stdout
    assert _cm_tenant_md(scratch_cm) == "a different deployment's rules\n"


def test_instructions_flag_rejects_a_missing_file(scratch_cm):
    result = _call_ensure(NS, scratch_cm, str(AGENTS_MD), "/no/such/file/anywhere.md")
    assert result.returncode != 0
    assert "no such file" in result.stderr
    assert not _cm_exists(scratch_cm), "must not create anything when the source file is missing"


# ============================================================== mount-shape tests (scratch pods)
# The claim this fix depends on: a ConfigMap mounted as a DIRECTORY reaches an
# already-running pod via the kubelet's sync; the same ConfigMap mounted with `subPath`
# does not. This is not asserted from documentation — both directions are measured here,
# on this cluster, because the prior version of this item shipped the wrong claim for the
# mount it actually used and no test caught it.

_PROBE_POD_DIR = """
apiVersion: v1
kind: Pod
metadata:
  name: {name}
  namespace: enterprise-ai
  labels: {{ app: cbf-mount-probe }}
spec:
  automountServiceAccountToken: false
  restartPolicy: Never
  containers:
    - name: probe
      image: busybox:1.36
      command: ["sh", "-c", "sleep 300"]
      volumeMounts:
        - {{ name: cm, mountPath: /mnt, readOnly: true }}
  volumes:
    - name: cm
      configMap: {{ name: {cm} }}
"""

_PROBE_POD_SUBPATH = """
apiVersion: v1
kind: Pod
metadata:
  name: {name}
  namespace: enterprise-ai
  labels: {{ app: cbf-mount-probe }}
spec:
  automountServiceAccountToken: false
  restartPolicy: Never
  containers:
    - name: probe
      image: busybox:1.36
      command: ["sh", "-c", "sleep 300"]
      volumeMounts:
        - {{ name: cm, mountPath: /mnt/TENANT.md, subPath: TENANT.md, readOnly: true }}
  volumes:
    - name: cm
      configMap: {{ name: {cm} }}
"""


def _apply_yaml(text: str):
    subprocess.run(["kubectl", "apply", "-f", "-"], input=text, text=True,
                    capture_output=True, timeout=30, check=True)


def _wait_running(pod: str, timeout_s: int = 60):
    import time as _t
    deadline = _t.time() + timeout_s
    while _t.time() < deadline:
        phase = _kubectl("get", "pod", pod, "-o", "jsonpath={.status.phase}", check=False).stdout
        if phase == "Running":
            return
        _t.sleep(2)
    raise TimeoutError(f"{pod} never reached Running")


def _exec_cat(pod: str, path: str) -> str:
    return _kubectl("exec", pod, "--", "cat", path, check=False).stdout


@pytest.mark.parametrize("shape", ["directory", "subpath"])
def test_configmap_mount_shape_determines_live_update(shape):
    """The load-bearing claim, measured both ways.

    A directory mount must reflect a ConfigMap update on an already-running pod. A
    subPath mount of the same key must NOT — proving why 61-workspace.template.yaml
    mounts /etc/opencode/tenant/ as a directory rather than subPath-ing TENANT.md
    directly, and why the prior "no restart needed" claim was false for the mount that
    version actually used.
    """
    import time as _t

    cm = f"cbf-mount-probe-cm-{uuid.uuid4().hex[:8]}"
    pod = f"cbf-mount-probe-{shape}-{uuid.uuid4().hex[:8]}"
    try:
        subprocess.run(
            ["kubectl", "-n", NS, "create", "configmap", cm, "--from-literal=TENANT.md=v1"],
            capture_output=True, text=True, timeout=30, check=True,
        )
        template = _PROBE_POD_DIR if shape == "directory" else _PROBE_POD_SUBPATH
        _apply_yaml(template.format(name=pod, cm=cm))
        _wait_running(pod)

        path = "/mnt/TENANT.md"
        before = _exec_cat(pod, path).strip()
        assert before == "v1"

        apply = subprocess.run(
            ["kubectl", "-n", NS, "create", "configmap", cm, "--from-literal=TENANT.md=v2",
             "--dry-run=client", "-o", "yaml"],
            capture_output=True, text=True, timeout=30, check=True,
        )
        subprocess.run(["kubectl", "apply", "-f", "-"], input=apply.stdout, text=True,
                        capture_output=True, timeout=30, check=True)

        deadline = _t.time() + 90
        saw_v2 = False
        while _t.time() < deadline:
            if _exec_cat(pod, path).strip() == "v2":
                saw_v2 = True
                break
            _t.sleep(5)

        if shape == "directory":
            assert saw_v2, "a directory-mounted ConfigMap must update on the kubelet's sync"
        else:
            assert not saw_v2, (
                "a subPath-mounted ConfigMap must NOT update without a pod restart — if "
                "this fails, Kubernetes' subPath behaviour changed and the claim in "
                "deploy/README.md / provision-workspace.sh / the volume comment needs "
                "re-checking against whichever mount shape is actually in use"
            )
    finally:
        _kubectl("delete", "pod", pod, "--ignore-not-found", "--wait=false", check=False)
        _delete_cm(cm)


def test_a_pod_starts_when_the_tenant_configmap_does_not_exist_at_all():
    """Defense in depth for done-condition 3: `optional: true` on the volume means a pod
    scheduled before any ConfigMap exists still starts (empty tenant dir), rather than
    hanging in ContainerCreating — the ordering guarantee (script creates the ConfigMap
    before the pod) should not be the ONLY thing standing between a fresh deployment and a
    pod that never comes up.
    """
    missing_cm = f"cbf-truly-missing-{uuid.uuid4().hex[:8]}"
    pod = f"cbf-optional-probe-{uuid.uuid4().hex[:8]}"
    assert not _cm_exists(missing_cm)
    template = """
apiVersion: v1
kind: Pod
metadata:
  name: {name}
  namespace: enterprise-ai
  labels: {{ app: cbf-mount-probe }}
spec:
  automountServiceAccountToken: false
  restartPolicy: Never
  containers:
    - name: probe
      image: busybox:1.36
      command: ["sh", "-c", "sleep 60"]
      volumeMounts:
        - {{ name: cm, mountPath: /etc/opencode/tenant, readOnly: true }}
  volumes:
    - name: cm
      configMap: {{ name: {cm}, optional: true }}
"""
    try:
        _apply_yaml(template.format(name=pod, cm=missing_cm))
        _wait_running(pod, timeout_s=60)
        listing = _kubectl("exec", pod, "--", "ls", "/etc/opencode/tenant", check=False).stdout
        assert "TENANT.md" not in listing
    finally:
        _kubectl("delete", "pod", pod, "--ignore-not-found", "--wait=false", check=False)


# ============================================================== platform-facts correctness
# A presence check ("the string 'no internet' appears somewhere in the file") is not a
# correctness check ("the pod actually has no internet"). This parses the real
# NetworkPolicy — both the checked-in YAML and the live object — and checks PLATFORM.md's
# claims against what the policy actually allows, rather than against what an earlier
# draft asserted.

def _egress_allows_unrestricted_internet(netpol_spec: dict) -> bool:
    """True if some egress rule reaches 0.0.0.0/0 (minus private ranges) on any port."""
    for rule in netpol_spec.get("egress", []):
        if "ports" in rule:
            continue  # port-restricted rules (DNS, gateway, TLS edge) are not "the internet"
        for to in rule.get("to", []):
            ipb = to.get("ipBlock", {})
            if ipb.get("cidr") == "0.0.0.0/0":
                return True
    return False


def test_checked_in_networkpolicy_matches_the_live_cluster():
    """If the repo and the running cluster disagree, that is its own finding — this test
    exists to catch it, not to paper over it with a value read from one side only.
    """
    checked_in = None
    for doc in yaml.safe_load_all(NETPOL_YAML.read_text()):
        if doc and doc.get("kind") == "NetworkPolicy":
            checked_in = doc["spec"]
    assert checked_in is not None, "no NetworkPolicy found in 60-workspace-common.yaml"

    live_raw = _kubectl("get", "networkpolicy", "workspace-isolation", "-o", "json").stdout
    live = json.loads(live_raw)["spec"]

    # Compare the egress section structurally (order-independent on the `to` list items,
    # which is all kubectl's own JSON round-trip is free to reorder).
    def _norm(rules):
        return sorted(json.dumps(r, sort_keys=True) for r in rules)

    assert _norm(checked_in["egress"]) == _norm(live["egress"]), (
        "the live workspace-isolation NetworkPolicy's egress rules do not match "
        "deploy/k8s/60-workspace-common.yaml — the cluster is running something the repo "
        "does not describe; file that as its own finding rather than trusting either side"
    )


def test_the_networkpolicy_genuinely_allows_general_internet_egress():
    """Ground truth for the claim PLATFORM.md must NOT make: verified structurally
    against the policy AND behaviourally against a live pod, not asserted from a comment.
    """
    spec = None
    for doc in yaml.safe_load_all(NETPOL_YAML.read_text()):
        if doc and doc.get("kind") == "NetworkPolicy":
            spec = doc["spec"]
    assert _egress_allows_unrestricted_internet(spec), (
        "expected the deliberate 0.0.0.0/0-minus-private egress rule described in "
        "60-workspace-common.yaml's own comments ('The internet, for pip / npm / git "
        "clone') — if this fails, the policy changed and PLATFORM.md's decision to drop "
        "the 'no internet' claim needs re-checking, not just this test"
    )

    # Behavioural confirmation against a real, currently-running workspace pod.
    pods = json.loads(
        _kubectl("get", "pod", "-l", "app.kubernetes.io/component=workspace", "-o", "json").stdout
    )["items"]
    running = [p["metadata"]["name"] for p in pods if p["status"].get("phase") == "Running"]
    assert running, "no running workspace pod to test egress from"
    probe = running[0]
    result = _kubectl(
        "exec", probe, "-c", "ttyd", "--", "curl", "-sS", "-o", "/dev/null",
        "-m", "8", "-w", "%{http_code}", "https://registry.npmjs.org/",
        check=False, timeout=20,
    )
    assert result.stdout.strip() == "200", (
        f"expected the pod to reach the internet (NetworkPolicy allows it); got "
        f"{result.stdout!r} / {result.stderr!r}"
    )


def test_platform_md_does_not_assert_the_false_network_claim():
    """Correctness, not presence: PLATFORM.md must not claim the pod has no internet route
    or that npm/pip installs fail, because both are false — proven by the two tests above.
    It IS allowed to claim no localStorage/sessionStorage in the preview, which is true
    (deploy/workspace/shell/index.html's iframe sandbox lacks allow-same-origin).
    """
    full_text = PLATFORM_MD.read_text()
    # Only the ASSERTED-facts section matters here — everything above the retrospective
    # heading, which explains in prose why the two false claims were dropped and
    # necessarily quotes them to do so. Quoting a false claim while explaining it is not
    # asserting it; the numbered list above the heading is the actual assertion surface.
    asserted_section = full_text.split("## What used to be here", 1)[0]
    text = asserted_section.lower()
    false_claims = [
        "there is no internet",
        "no egress",
        "never run `npm install`",
        "never run npm install",
    ]
    for claim in false_claims:
        assert claim not in text.replace("*", ""), (
            f"PLATFORM.md must not assert {claim!r} as a platform fact — it is baked "
            "into the image and this repo has already measured it false against the "
            "live NetworkPolicy (see the section explaining why it was dropped)"
        )
    assert "localstorage" in text and "throw" in text, (
        "PLATFORM.md should still carry the one verified platform fact"
    )


def test_index_html_sandbox_actually_lacks_allow_same_origin():
    """Ground truth for the one fact PLATFORM.md DOES keep."""
    index_html = REPO / "deploy/workspace/shell/index.html"
    import re

    m = re.search(r'<iframe[^>]*\bsandbox="([^"]*)"', index_html.read_text())
    assert m, "no sandboxed iframe found in deploy/workspace/shell/index.html"
    assert "allow-same-origin" not in m.group(1).split(), (
        "the preview iframe gained allow-same-origin — localStorage/sessionStorage no "
        "longer throw there, so PLATFORM.md's remaining platform fact is now false too"
    )


# ============================================================== the control case (done-condition 3)
# "A pod provisioned WITHOUT the new configuration still gets working house rules" — the
# guarantee used to be BY CONSTRUCTION (AGENTS.md COPYed into the image). It is now
# provided by deploy/bin/provision-workspace.sh always seeding the ConfigMap before the
# pod is created. This drives the REAL script end to end and checks the REAL pod.

@pytest.fixture(scope="module")
def cbf_users():
    for user, email in (("cbftest", "cbftest@example.invalid"),
                         ("cbftest2", "cbftest2@example.invalid")):
        subprocess.run(
            [str(REPO / "deploy/bin/ensure-second-user.sh"), user, email],
            capture_output=True, text=True, timeout=120, check=True,
        )
    return ("cbftest", "cbftest2")


def _provision(user: str, *extra_args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["WORKSPACE_IMAGE"] = TEST_IMAGE
    return subprocess.run(
        [str(REPO / "deploy/bin/provision-workspace.sh"), user, *extra_args],
        capture_output=True, text=True, timeout=300, cwd=str(REPO), env=env,
    )


def _pod_for(user: str) -> str:
    return _kubectl("get", "pod", "-l", f"workspace.enterprise-ai/user={user}",
                     "-o", "jsonpath={.items[0].metadata.name}").stdout


def _snapshot_cm(name: str) -> dict:
    """Capture enough state to restore `name` EXACTLY: whether it existed, and if so, its
    TENANT.md content verbatim.

    Wave-5 finding: a restore that always writes the camp default (deploy/workspace/
    AGENTS.md) is wrong whenever an operator has put their own house rules in this
    ConfigMap — which is the entire reason this item exists. The default is only correct
    when the ConfigMap genuinely did not exist before the test touched it.
    """
    if _cm_exists(name):
        return {"existed": True, "content": _cm_tenant_md(name)}
    return {"existed": False, "content": None}


def _restore_cm_snapshot(name: str, snapshot: dict, default_seed: Path) -> None:
    """Force `name` back to exactly what `snapshot` describes, then VERIFY BY READING THE
    CONFIGMAP'S CONTENT BACK — not by trusting a subprocess return code.

    Wave-5 finding (1), "the guard is vacuous": the previous version asserted
    `restore.returncode == 0` on the CompletedProcess from `_call_ensure`. `_call_ensure`
    runs `ensure_tenant_instructions` via `bash -c` with no `set -euo pipefail`, and that
    function's LAST statement is `echo` — so the subshell's exit status is echo's, not
    kubectl's. That assertion passed whether or not the ConfigMap was actually written.
    Reading `.data.TENANT\\.md` back and comparing it against what we intended to leave
    behind cannot be fooled by an unrelated command's exit code the way a `bash -c` return
    code can — see test_restore_guard_actually_checks_configmap_content_not_a_return_code
    below, which proves it by forcing exactly that failure shape.

    Wave-5 finding (2), "restores the wrong thing": if `name` HELD something before the
    test ran, THAT EXACT CONTENT is restored — never the camp default. The default is used
    only when `name` genuinely did not exist beforehand (a deployment that has never been
    configured), which is the one case restoring the default is correct.
    """
    if snapshot["existed"]:
        expected = snapshot["content"]
        tmp = Path(f"/tmp/cbf-restore-{uuid.uuid4().hex[:8]}.md")
        tmp.write_text(expected)
        try:
            _call_ensure(NS, name, str(default_seed), str(tmp))
        finally:
            tmp.unlink(missing_ok=True)
    else:
        _delete_cm(name)  # in case a test (re-)seeded it after the snapshot was taken
        expected = default_seed.read_text()
        _call_ensure(NS, name, str(default_seed))

    actual = _cm_tenant_md(name)
    assert actual == expected, (
        f"FAILED TO RESTORE {name} to its pre-test state — the live deployment's house "
        f"rules do not match what was there before this test ran. "
        f"expected={expected!r} actual={actual!r}"
    )


@pytest.fixture()
def prod_cm_snapshot():
    """Snapshot PROD_CM before a test touches it, and force it back to EXACTLY that
    snapshot afterward — pytest runs fixture teardown (the code after `yield`)
    unconditionally once setup has completed, whether the test passes, fails an
    assertion, or raises, the same guarantee a `try/finally` around the test body gives.
    Centralising it here means the restore logic (and its fault-injection proof) lives in
    one place instead of being re-implemented inside every test that mutates PROD_CM.

    See test_restore_guard_recovers_the_exact_pre_test_content_after_a_forced_mid_test_failure
    and test_restore_guard_actually_checks_configmap_content_not_a_return_code below for
    fault-injection proof this actually restores the right content and actually verifies
    it worked, rather than trusting a return code that cannot fail.
    """
    snapshot = _snapshot_cm(PROD_CM)
    yield snapshot
    _restore_cm_snapshot(PROD_CM, snapshot, AGENTS_MD)


def test_fresh_deployment_pod_gets_the_camp_rules_verbatim_with_no_operator_action(
    cbf_users, prod_cm_snapshot
):
    """The control case. Delete the shared ConfigMap to simulate a genuinely fresh
    deployment (nobody has ever run --instructions), then provision a user with NO flags —
    the path every existing camp deployment takes today — and check the resulting pod, not
    just the ConfigMap.

    PROD_CM is the real, shared, deployment-wide ConfigMap every workspace pod mounts —
    there is no throwaway substitute available here because provision-workspace.sh always
    names it literally (it is not parameterised). The `prod_cm_snapshot` fixture snapshots
    PROD_CM's actual pre-test state (an operator's own content if there is one, not the
    camp default) before this test runs, and restores exactly that in its teardown, which
    pytest guarantees runs whether `_provision` fails, an assert below fails, or everything
    passes. A wave-3 review found the unguarded version of this test could leave the live
    tenant's house rules deleted on any failure path; a wave-5 review found the
    first-round fix restored the wrong content on the success path (the camp default
    instead of whatever was actually there) and did so behind a return-code assertion that
    could not fail either way — both are fixed in `_restore_cm_snapshot` above.
    """
    user, _ = cbf_users
    _delete_cm(PROD_CM)
    assert not _cm_exists(PROD_CM)

    result = _provision(user)
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "seeded default" in result.stdout

    pod = _pod_for(user)
    assert pod, f"no pod for {user}"

    tenant_md = _exec_cat(pod, "/etc/opencode/tenant/TENANT.md")
    assert tenant_md == AGENTS_MD.read_text(), (
        "the pod's mounted TENANT.md must equal the camp's AGENTS.md byte-for-byte — "
        "this is done-condition 3"
    )

    platform_md = _exec_cat(pod, "/etc/opencode/PLATFORM.md")
    assert platform_md == PLATFORM_MD.read_text()

    cfg = _kubectl("exec", pod, "-c", "ttyd", "--", "opencode", "debug", "config", check=False)
    assert cfg.returncode == 0, cfg.stderr
    resolved = json.loads(cfg.stdout)
    assert resolved["instructions"] == [
        "/etc/opencode/PLATFORM.md", "/etc/opencode/tenant/TENANT.md"
    ]


def test_a_second_user_with_different_instructions_proves_per_deployment_not_per_user(
    cbf_users, prod_cm_snapshot
):
    """Done-condition 4: a second, different instruction set on a second user, and it must
    be visible on the FIRST user's already-running pod too — because the scope decision
    (per-deployment, not per-user) means there is exactly one ConfigMap.

    This test overwrites PROD_CM (the real, shared, deployment-wide ConfigMap — there is no
    throwaway substitute, provision-workspace.sh always names it literally) with a
    throwaway test string partway through. Same hazard class as the fresh-deployment test
    above: if any assert between the overwrite and the end of the test failed, PROD_CM
    would be left holding test content instead of the deployment's real house rules on
    that failure path. The `prod_cm_snapshot` fixture snapshots PROD_CM's real pre-test
    content and restores exactly that in its teardown — guaranteed by pytest to run
    whether this test passes or fails — rather than the camp default, which would be wrong
    on any deployment where an operator has already set their own instructions.
    """
    user1, user2 = cbf_users
    pod1_before = _pod_for(user1)
    restarts_before = _kubectl(
        "get", "pod", pod1_before, "-o",
        "jsonpath={.status.containerStatuses[0].restartCount}",
    ).stdout

    custom = "# A different deployment's rules\n\nProduction code, full paragraphs.\n"
    tmp = Path("/tmp") / f"cbf-custom-{uuid.uuid4().hex[:8]}.md"
    tmp.write_text(custom)
    try:
        result = _provision(user2, "--instructions", str(tmp))
        assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
        assert "updated" in result.stdout
    finally:
        tmp.unlink(missing_ok=True)

    pod2 = _pod_for(user2)
    assert _exec_cat(pod2, "/etc/opencode/tenant/TENANT.md") == custom

    # The already-running FIRST user's pod must pick this up too — same pod, no
    # restart — because it is one ConfigMap for the whole deployment.
    import time as _t

    deadline = _t.time() + 90
    seen = ""
    while _t.time() < deadline:
        seen = _exec_cat(pod1_before, "/etc/opencode/tenant/TENANT.md")
        if seen == custom:
            break
        _t.sleep(5)
    assert seen == custom, (
        f"user1's already-running pod never saw the deployment-wide update; last "
        f"read: {seen!r}"
    )

    restarts_after = _kubectl(
        "get", "pod", pod1_before, "-o",
        "jsonpath={.status.containerStatuses[0].restartCount}",
    ).stdout
    assert restarts_after == restarts_before, (
        "the update must reach the running pod without a restart"
    )


# ============================================================== fault-injection proof for the restore guard
# Wave 5 found the guard behind prod_cm_snapshot's predecessor (_restore_prod_default)
# vacuous in two distinct ways:
#
#   (1) `assert restore.returncode == 0` could not fail. `_call_ensure` runs
#       `ensure_tenant_instructions` via `bash -c` with no `set -euo pipefail`, and that
#       function's LAST statement is `echo` — the subshell's exit status is echo's, not
#       kubectl's. The assertion passed whether or not the ConfigMap was actually written.
#
#   (2) even on the genuine success path it restored the CAMP DEFAULT rather than
#       whatever was actually there before the test ran — which silently destroys a real
#       operator's configuration, on every run, on any deployment where one exists. This
#       item exists specifically so an operator CAN put their own house rules in this
#       ConfigMap; a suite that resets it to the camp default is the exact bug the item
#       is meant to fix, reintroduced by the guard meant to protect against it.
#
# Both are fixed in `_snapshot_cm` / `_restore_cm_snapshot` above. These two tests force
# each failure mode for real — using scratch ConfigMaps, never PROD_CM — and prove the
# fixed code behaves correctly where the old code would have passed silently.

def test_restore_guard_recovers_the_exact_pre_test_content_after_a_forced_mid_test_failure():
    """Fault-injection proof for finding (2) plus the try/finally-equivalent guarantee
    `prod_cm_snapshot` gives every test that uses it: seed a scratch ConfigMap with
    OPERATOR content (deliberately NOT the camp default), snapshot it the way the fixture
    does, mutate it the way a real control-case test's body does, then force an assertion
    failure midway through — the same shape as a real assert failing inside
    test_fresh_deployment_pod_gets_the_camp_rules_verbatim_with_no_operator_action or
    test_a_second_user_with_different_instructions_proves_per_deployment_not_per_user —
    and prove the ConfigMap comes back holding its ORIGINAL operator content: not the camp
    default, and not the mid-test mutation.
    """
    cm = f"cbf-fault-inject-{uuid.uuid4().hex[:10]}"
    operator_content = "# this deployment's real house rules\n\nnot the camp default.\n"
    tmp = Path("/tmp") / f"cbf-operator-{uuid.uuid4().hex[:8]}.md"
    tmp.write_text(operator_content)
    try:
        _call_ensure(NS, cm, str(AGENTS_MD), str(tmp))
        assert _cm_tenant_md(cm) == operator_content, "setup: seeding operator content failed"
        assert operator_content != AGENTS_MD.read_text(), (
            "test invariant: operator content must differ from the camp default, or this "
            "test cannot tell 'restored correctly' apart from 'restored the default'"
        )

        snapshot = _snapshot_cm(cm)
        assert snapshot == {"existed": True, "content": operator_content}

        def _mid_test_body():
            # Mutate the CM the way a real control-case test's body does, then hit the
            # kind of assertion failure a real regression would cause.
            _call_ensure(NS, cm, str(AGENTS_MD), str(AGENTS_MD))
            assert _cm_tenant_md(cm) == AGENTS_MD.read_text()
            raise AssertionError("INJECTED: simulating a real assertion failing mid-test")

        try:
            _mid_test_body()
            pytest.fail("the injected failure did not fire; this test proves nothing")
        except AssertionError as e:
            assert "INJECTED" in str(e), f"unexpected assertion failure: {e}"
        finally:
            # This is the exact call prod_cm_snapshot's fixture teardown makes.
            _restore_cm_snapshot(cm, snapshot, AGENTS_MD)

        assert _cm_tenant_md(cm) == operator_content, (
            "the restore guard did not recover the pre-test content after a forced "
            "mid-test failure — it must, whether the test's own assertions failed or not"
        )
    finally:
        tmp.unlink(missing_ok=True)
        _delete_cm(cm)


def test_restore_guard_actually_checks_configmap_content_not_a_return_code(monkeypatch):
    """Fault-injection proof for finding (1): the OLD guard was
    `assert restore.returncode == 0` on the CompletedProcess `_call_ensure` returns. This
    monkeypatches `_call_ensure` to return exactly the failure shape that made the old
    guard vacuous — a "succeeded" CompletedProcess (returncode 0, the success message on
    stdout) that changes nothing — and proves `_restore_cm_snapshot`, which reads the
    ConfigMap's content back instead of trusting the return code, raises where the old
    code would have passed silently.
    """
    cm = f"cbf-fault-inject-{uuid.uuid4().hex[:10]}"
    original = "# original content the fake restore call must not touch\n"
    stale = "# stale content the fake restore call leaves behind\n"
    try:
        subprocess.run(
            ["kubectl", "-n", NS, "create", "configmap", cm, f"--from-literal=TENANT.md={stale}"],
            capture_output=True, text=True, timeout=30, check=True,
        )
        assert _cm_tenant_md(cm) == stale

        snapshot = {"existed": True, "content": original}

        def fake_call_ensure(*_args, **_kwargs):
            """Simulates the exact wave-5 failure shape: a `bash -c "...; echo ..."`
            subshell reporting success because echo succeeded, while the kubectl commands
            before it did nothing (e.g. a swallowed apiserver error) — the ConfigMap is
            left at `stale`, not restored to `original`.
            """
            return subprocess.CompletedProcess(
                args=[], returncode=0,
                stdout="    instructions ... (deployment-wide, updated)\n", stderr="",
            )

        monkeypatch.setattr(sys.modules[__name__], "_call_ensure", fake_call_ensure)

        with pytest.raises(AssertionError, match="FAILED TO RESTORE"):
            _restore_cm_snapshot(cm, snapshot, AGENTS_MD)

        # The fake never touched the real ConfigMap — still `stale`, proving the
        # assertion above fired on a genuine content mismatch, not a coincidence.
        assert _cm_tenant_md(cm) == stale, (
            "sanity check: the fake _call_ensure must not have actually changed the "
            "ConfigMap, or this test is not isolating the guard's own logic"
        )
    finally:
        _delete_cm(cm)
