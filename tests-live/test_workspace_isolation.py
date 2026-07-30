"""Only the portal may reach a workspace, and only your own is served to you.

The workspace pods lost their per-pod oauth2-proxy when the workshop became a tab: the
portal authenticates once, on one origin, and proxies you to your own pod. Reaching a
workspace's ttyd is reaching a shell that holds a spendable key, so what replaced that
sidecar is tested here rather than asserted in a comment.

The NetworkPolicy is the part most likely to be wrong, because of a CNI behaviour already
recorded in 60-workspace-common.yaml: kube-router resolves a packet on the DESTINATION
pod's ingress rules WITHOUT consulting the source's egress. An ingress rule whose `from`
list is missing or too broad therefore opens every workspace to every other workspace,
while the egress section looks like it forbids exactly that.

WHOSE PODS (enterpriseaiframework-cf5)

This file used to name `claire` and `student` — two people — as the source and destination
of every probe, and it spawns a process inside the source pod to make them. It now takes the
pair from live_identity.py, so the probes run between two throwaway workspaces and the
claim is unchanged: the pair is what makes it a real test, not which two accounts they are.

Marked per test, not per module: `test_the_workspace_ports_are_not_published_on_any_node`
reads Services by label and needs no account at all, so it stays runnable at any hour. That
matters — a NodePort appearing on a workspace Service routes around the NetworkPolicy
entirely, and it is the one claim here worth being able to check on a whim.
"""

import json
import subprocess

import pytest

import live_identity

NS = "enterprise-ai"


def _kubectl(*args, check=True):
    return subprocess.run(["kubectl", "-n", NS, *args],
                          capture_output=True, text=True, timeout=120, check=check).stdout


def _pod(user: str) -> str:
    return _kubectl("get", "pod", "-l", f"workspace.enterprise-ai/user={user}",
                    "-o", "jsonpath={.items[0].metadata.name}")


@pytest.fixture(scope="module")
def pair(request) -> tuple[str, str]:
    """(source, destination) — two throwaway workspaces, never a person's."""
    src, dst = live_identity.account(request)[0], live_identity.second_account(request)[0]
    assert src != dst, (
        f"both principals resolved to {src!r}; a workspace reaching ITSELF proves nothing "
        f"— set {live_identity.ENV_USER_2} to a different throwaway account"
    )
    return src, dst


PROBE = """
import socket, sys, json
out = {}
for port in (7681, 7682):
    s = socket.socket(); s.settimeout(6)
    try:
        s.connect((sys.argv[1], port)); out[port] = "open"
    except Exception as e:
        out[port] = type(e).__name__
    finally:
        s.close()
print(json.dumps(out))
"""


@pytest.mark.needs_real_user
def test_a_workspace_cannot_reach_another_workspace(pair):
    """The one that matters. A shell on the pod network must not find another shell."""
    src_user, dst_user = pair
    src = _pod(src_user)
    assert src, f"no pod for {src_user}; the probe would be vacuous"
    out = subprocess.run(
        ["kubectl", "-n", NS, "exec", "-i", src, "-c", "ttyd", "--",
         "python3", "-c", PROBE, f"ws-{dst_user}"],
        capture_output=True, text=True, timeout=120,
    )
    result = json.loads(out.stdout.strip().splitlines()[-1])
    assert result["7681"] != "open", (
        "one workspace reached another's TERMINAL — that is a shell holding somebody "
        f"else's spendable key. Probe said: {result}"
    )
    assert result["7682"] != "open", f"one workspace reached another's shell API: {result}"


def test_the_workspace_ports_are_not_published_on_any_node():
    """A NodePort would route around the NetworkPolicy entirely.

    No account and no pod: this reads Service objects by label, so it is safe to run while
    somebody is working and is deliberately left ungated.
    """
    svcs = json.loads(_kubectl("get", "svc", "-l",
                               "app.kubernetes.io/component=workspace", "-o", "json"))
    assert svcs["items"], (
        "no workspace Services matched the label at all, so this test would pass on a "
        "cluster with none — check the selector before believing it"
    )
    for svc in svcs["items"]:
        assert svc["spec"]["type"] == "ClusterIP", (
            f"{svc['metadata']['name']} is {svc['spec']['type']}; a workspace must not be "
            "reachable from outside the cluster"
        )
        for port in svc["spec"]["ports"]:
            assert "nodePort" not in port, f"{svc['metadata']['name']} publishes {port}"


@pytest.mark.needs_real_user
def test_the_shell_api_refuses_a_request_without_the_token(pair):
    """Reaching the port must not be the same as using it."""
    _, dst_user = pair
    probe = (
        "import urllib.request, urllib.error, sys\n"
        f"req = urllib.request.Request('http://ws-{dst_user}:7682/api/state')\n"
        "try:\n"
        "    urllib.request.urlopen(req, timeout=8); print('ACCEPTED')\n"
        "except urllib.error.HTTPError as e: print('REFUSED', e.code)\n"
        "except Exception as e: print('BLOCKED', type(e).__name__)\n"
    )
    out = subprocess.run(
        ["kubectl", "-n", NS, "exec", "-i", "deploy/control-plane", "-c", "control-plane",
         "--", "python3", "-c", probe],
        capture_output=True, text=True, timeout=120,
    ).stdout
    assert "ACCEPTED" not in out, f"the shell API served an unauthenticated request: {out}"
    # The probe has to have RUN. Without this, a kubectl exec that failed for any reason
    # produces empty output, "ACCEPTED" is not in it, and the test passes having measured
    # nothing — which is the exact shape of the defects this repo keeps finding.
    assert "REFUSED" in out or "BLOCKED" in out, (
        f"the probe produced no verdict at all, so it proves nothing: {out!r}"
    )


@pytest.mark.needs_real_user
def test_the_control_plane_can_reach_a_workspace_with_the_token(pair):
    """The other half: the guard must not be so tight that the product does not work."""
    _, dst_user = pair
    probe = (
        "import os, urllib.request\n"
        f"req = urllib.request.Request('http://ws-{dst_user}:7682/api/state',\n"
        "    headers={'X-Workspace-Token': os.environ['WORKSPACE_INTERNAL_TOKEN']})\n"
        "print(urllib.request.urlopen(req, timeout=8).status)\n"
    )
    out = subprocess.run(
        ["kubectl", "-n", NS, "exec", "-i", "deploy/control-plane", "-c", "control-plane",
         "--", "python3", "-c", probe],
        capture_output=True, text=True, timeout=120,
    ).stdout
    assert "200" in out, f"the portal cannot reach a workspace it is supposed to serve: {out}"
