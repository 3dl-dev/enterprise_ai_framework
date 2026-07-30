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
"""

import base64
import json
import subprocess
import uuid

import pytest

NS = "enterprise-ai"


def _kubectl(*args, check=True):
    return subprocess.run(["kubectl", "-n", NS, *args],
                          capture_output=True, text=True, timeout=120, check=check).stdout


def _secret(name: str, key: str) -> str:
    return base64.b64decode(_kubectl("get", "secret", name, "-o",
                                     f"jsonpath={{.data.{key}}}")).decode()


def _pod(user: str) -> str:
    return _kubectl("get", "pod", "-l", f"workspace.enterprise-ai/user={user}",
                    "-o", "jsonpath={.items[0].metadata.name}")


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


def test_a_workspace_cannot_reach_another_workspace(): 
    """The one that matters. A shell on the pod network must not find another shell."""
    src = _pod("claire")
    out = subprocess.run(
        ["kubectl", "-n", NS, "exec", "-i", src, "-c", "ttyd", "--",
         "python3", "-c", PROBE, "ws-student"],
        capture_output=True, text=True, timeout=120,
    )
    result = json.loads(out.stdout.strip().splitlines()[-1])
    assert result["7681"] != "open", (
        "one workspace reached another's TERMINAL — that is a shell holding somebody "
        f"else's spendable key. Probe said: {result}"
    )
    assert result["7682"] != "open", f"one workspace reached another's shell API: {result}"


def test_the_workspace_ports_are_not_published_on_any_node():
    """A NodePort would route around the NetworkPolicy entirely."""
    svcs = json.loads(_kubectl("get", "svc", "-l",
                               "app.kubernetes.io/component=workspace", "-o", "json"))
    for svc in svcs["items"]:
        assert svc["spec"]["type"] == "ClusterIP", (
            f"{svc['metadata']['name']} is {svc['spec']['type']}; a workspace must not be "
            "reachable from outside the cluster"
        )
        for port in svc["spec"]["ports"]:
            assert "nodePort" not in port, f"{svc['metadata']['name']} publishes {port}"


def test_the_shell_api_refuses_a_request_without_the_token():
    """Reaching the port must not be the same as using it."""
    probe = (
        "import urllib.request, urllib.error, sys\n"
        "req = urllib.request.Request('http://ws-student:7682/api/state')\n"
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


def test_the_control_plane_can_reach_a_workspace_with_the_token():
    """The other half: the guard must not be so tight that the product does not work."""
    probe = (
        "import os, urllib.request\n"
        "req = urllib.request.Request('http://ws-student:7682/api/state',\n"
        "    headers={'X-Workspace-Token': os.environ['WORKSPACE_INTERNAL_TOKEN']})\n"
        "print(urllib.request.urlopen(req, timeout=8).status)\n"
    )
    out = subprocess.run(
        ["kubectl", "-n", NS, "exec", "-i", "deploy/control-plane", "-c", "control-plane",
         "--", "python3", "-c", probe],
        capture_output=True, text=True, timeout=120,
    ).stdout
    assert "200" in out, f"the portal cannot reach a workspace it is supposed to serve: {out}"


# ===========================================================================
# enterpriseaiframework-784 — the workspace's OUTBOUND authority.
#
# Everything above is about who may reach INTO a workspace. The egress direction became a
# live question when the terminal agent needed the MCP servers the chat surface uses
# (-471): the workspace NetworkPolicy permitted DNS, the gateway and the public internet,
# so every tool chat gained was invisible to that surface.
#
# The rule added for it names ONE service by its own `app` label and its own port. The
# reason it is not simply "the namespace" or "the pod CIDR" is the CNI behaviour recorded
# at the top of this file and in the manifest: a destination wide enough to contain
# workspace pods re-opens workspace-to-workspace traffic — the exact thing the ingress
# `from` lists exist to close — while the policy still reads as though it forbade it. So
# the negative case is measured here, not assumed, and it is measured on a service that
# listens on the SAME port as the one that was named, so "we opened 8080 to everything"
# cannot pass as "we opened mcp-echo".
#
# Shape, tested hermetically and independently: tests/test_workspace_egress_allowlist.py.
# ===========================================================================

# Everything in the namespace a workspace must NOT be able to open a connection to,
# with what each one would hand the shell if it could. `fakeprovider` is the discriminator:
# same port as the MCP server that IS allowed, and deliberately absent from the rule.
FORBIDDEN_IN_NAMESPACE = {
    ("fakeprovider", 8080): "an in-namespace service on the same port as the allowed one",
    ("control-plane", 8000): "the control plane, which mints and revokes virtual keys",
    ("postgres", 5432): "the ledger and audit chain, directly",
    ("identity", 8080): "the identity provider's internal service",
    ("valkey", 6379): "the gateway's cache and rate-limit state",
    ("chat", 3080): "another user's chat surface",
}

# The one that IS allowed.
MCP_SERVER = ("mcp-echo", 8080)

TCP_PROBE = """
import socket, sys, json
out = {}
for spec in sys.argv[1:]:
    host, _, port = spec.rpartition(":")
    s = socket.socket(); s.settimeout(6)
    try:
        s.connect((host, int(port))); out[spec] = "open"
    except Exception as e:
        out[spec] = type(e).__name__
    finally:
        s.close()
print(json.dumps(out))
"""

# A real MCP session over streamable-http, stdlib only, because a workspace pod has no
# httpx. Reaching the port is not the same as the tool running: `initialize` then
# `tools/list` then `tools/call`, and the reply must carry `ECHO:<nonce>` — a value the
# probe cannot produce on its own and that nothing can have cached, which is what makes
# this the tool ACTUALLY RUNNING rather than the port being open.
MCP_PROBE = r"""
import json, sys, urllib.request

BASE = sys.argv[1].rstrip("/")
NONCE = sys.argv[2]
PROTO = "2025-06-18"


def post(body, session=None, notify=False):
    headers = {"Content-Type": "application/json",
               "Accept": "application/json, text/event-stream",
               "MCP-Protocol-Version": PROTO}
    if session:
        headers["Mcp-Session-Id"] = session
    req = urllib.request.Request(BASE + "/mcp", data=json.dumps(body).encode(),
                                 headers=headers, method="POST")
    resp = urllib.request.urlopen(req, timeout=20)
    sid = resp.headers.get("Mcp-Session-Id")
    raw = resp.read().decode()
    if notify:
        return sid, None
    for line in raw.splitlines():
        if line.startswith("data:"):
            return sid, json.loads(line[5:].strip())
    return sid, json.loads(raw) if raw.strip() else None


out = {}
try:
    sid, init = post({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                      "params": {"protocolVersion": PROTO, "capabilities": {},
                                 "clientInfo": {"name": "isolation-probe", "version": "0"}}})
    out["server"] = init["result"]["serverInfo"]["name"]
    post({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid, notify=True)
    _, listed = post({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, sid)
    out["tools"] = sorted(t["name"] for t in listed["result"]["tools"])
    _, called = post({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                      "params": {"name": "echo", "arguments": {"text": NONCE}}}, sid)
    out["echo"] = called["result"]["content"][0]["text"]
    out["ok"] = True
except Exception as exc:
    out["ok"] = False
    out["error"] = "%s: %s" % (type(exc).__name__, exc)
print(json.dumps(out))
"""


def _in_workspace(user: str, script: str, *args: str) -> str:
    """Run a script inside a workspace's shell container. stdin, not `-c`: the MCP probe is
    too long to pass as an argument cleanly and quoting it through kubectl exec is how a
    probe silently becomes a syntax error that reads as a network failure."""
    done = subprocess.run(
        ["kubectl", "-n", NS, "exec", "-i", _pod(user), "-c", "ttyd",
         "--", "python3", "-", *args],
        input=script, capture_output=True, text=True, timeout=180,
    )
    assert done.stdout.strip(), (
        f"the probe printed nothing (rc={done.returncode}); it did not run, so this is not "
        f"evidence about the network either way: {done.stderr[-600:]}"
    )
    return done.stdout.strip().splitlines()[-1]


def test_a_workspace_can_call_the_mcp_server_the_chat_surface_uses():
    """DONE CONDITION (1) of -784. The tool call, not the open port.

    RED until deploy/k8s/60-workspace-common.yaml's mcp-echo egress rule is applied to the
    cluster; that apply is the escalation on this item. Before it, this fails with a
    connection error at `initialize`, which is exactly the state the item was filed in.
    """
    nonce = f"784-{uuid.uuid4().hex[:12]}"
    result = json.loads(_in_workspace("student", MCP_PROBE,
                                      f"http://{MCP_SERVER[0]}:{MCP_SERVER[1]}", nonce))
    assert result["ok"], (
        "a workspace pod could not complete an MCP session against "
        f"{MCP_SERVER[0]}:{MCP_SERVER[1]} — the terminal agent has none of the tools the "
        f"chat surface has (enterpriseaiframework-784/-471): {result.get('error')}"
    )
    assert result["tools"] == ["echo"], f"unexpected tool list: {result}"
    assert result["echo"] == f"ECHO:{nonce}", (
        "the MCP server was reachable but the tool did not run on this input — "
        f"expected ECHO:{nonce}, got {result.get('echo')!r}. A reachable port is not a "
        "working tool call."
    )


def test_a_workspace_cannot_reach_an_in_namespace_service_absent_from_the_egress_rule():
    """DONE CONDITION (3) of -784, and the whole reason the rule names one service.

    `fakeprovider:8080` is in the same namespace, on the same port as the MCP server that
    IS allowed, and is not named. If it answers, the grant was made by port or by
    namespace rather than by destination — and by this cluster's CNI behaviour the same
    mistake would have handed every workspace a route to every other workspace's shell.

    The rest are the claims the manifest has asserted in a comment since the surface was
    created ("the pod cannot reach the control plane, the identity provider's internal
    service, Postgres, or another workspace") and that nothing measured until now. They
    are the cases -784 did NOT change, which is why they are checked in the same run: a
    widening bug shows up here first.
    """
    specs = [f"{h}:{p}" for h, p in FORBIDDEN_IN_NAMESPACE]
    result = json.loads(_in_workspace("student", TCP_PROBE, *specs))
    opened = {spec: FORBIDDEN_IN_NAMESPACE[(spec.rsplit(":", 1)[0], int(spec.rsplit(":", 1)[1]))]
              for spec, state in result.items() if state == "open"}
    assert not opened, (
        "a workspace pod — a shell the user controls, running code an agent wrote, holding "
        "a spendable virtual key — reached services it is not permitted to reach. Each one "
        f"is an authority it should not have: {opened}. Full probe: {result}"
    )


def test_the_allowed_destinations_are_reachable_so_the_guard_is_not_vacuous():
    """The other half of the negative above.

    Without this, a workspace pod with no network at all — a broken CNI, a pod that failed
    to get an address, a probe that never ran — would pass every isolation assertion in
    this file and read as perfect security. Both allowed destinations must answer.
    """
    result = json.loads(_in_workspace(
        "student", TCP_PROBE, "gateway:4000", f"{MCP_SERVER[0]}:{MCP_SERVER[1]}"))
    assert result["gateway:4000"] == "open", (
        "a workspace cannot reach the gateway, which is its only route to a model — so the "
        f"negative results in this file prove nothing about policy: {result}"
    )
    assert result[f"{MCP_SERVER[0]}:{MCP_SERVER[1]}"] == "open", (
        f"the MCP server is unreachable from a workspace: {result}. Apply the mcp-echo "
        "egress rule in deploy/k8s/60-workspace-common.yaml (enterpriseaiframework-784)."
    )
