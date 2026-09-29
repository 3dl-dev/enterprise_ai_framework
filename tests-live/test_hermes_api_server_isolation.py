"""The hermes API server (:8642) answers the control plane and nobody else (enterpriseaiframework-147, -d95).

Run against the live k3s cluster:  pytest tests-live/test_hermes_api_server_isolation.py

Mock-free. It renders the REAL 65-agent-hermes.template.yaml by literal placeholder
substitution, applies it beside a THROWAWAY copy of the REAL 66-agent-console-common.yaml
policy, and measures:

  * from the control-plane pod, POST /v1/chat/completions with the agent's own Bearer secret
    returns a model reply;
  * the same request without (or with a wrong) secret is rejected by hermes: the port has
    its own auth on top of the network fence;
  * from an agent-labelled pod and from a Raven-labelled pod, whose OWN egress is opened to
    the target (a throwaway egress policy), the TCP connection to :8642 fails at the network
    level. Because their egress is open, only the destination fence can be what refuses them.
    The test proves that: it mutates the fence (removed; widened to component=agent; widened
    to type=raven) and requires the refusals to turn into connections exactly as MATRIX says.

The target agent is relabelled component=agent-d95 and the fence under test is a throwaway
copy of the real 66 policy (ingress rule read from the file; name and podSelector rewritten),
so the shared live agent-console-isolation policy is never applied, overwritten or relied on
(enterpriseaiframework-27a) and no shared policy selects the target.

Expected values are the hand-written MATRIX (derived from what each fence state admits) and
the key this test wrote into the Secret the agent started with; "refused" is a transport
failure observed from the source pod. Throwaway objects are `*swtestd95*` / `*-d95`, removed
even on failure. The agent borrows the model config and virtual key of an existing hermes
agent so inference works.
"""
import base64
import json
import os
import subprocess
import time
from pathlib import Path

import pytest
import yaml

NS = "enterprise-ai"
USER, NAME = "swtestd95", "probe"
OBJ = f"agent-{USER}-{NAME}"
REPO = Path(__file__).resolve().parent.parent
DONOR = "agent-baron-chad"  # a live hermes agent: donates model config + a spendable key
TARGET_COMPONENT = "agent-d95"  # selected by no shared policy; only the throwaway fence selects it
PROBE_LABEL = "d95-probe"
FENCE = "agent-console-isolation-d95"
EGRESS = "d95-probe-egress"
OTHERAGENT = "otheragent-swtestd95"  # wears what every hermes/openclaw agent pod wears
RAVEN = "raven-swtestd95"            # wears what a Raven pod will wear (agents-raven.md Contract E)
BARE = "bare-swtestd95"              # wears nothing but the probe label
PROBES = {"other-agent": OTHERAGENT, "raven": RAVEN, "bare": BARE}
AGENT_LBL = "app.kubernetes.io/component=agent"

# What each fence state must do to each probe. Hand-derived from what the state admits, NOT
# produced by the code under test. Every probe's egress to :8642 is open, so a NETFAIL can only
# be the destination fence; CONNECTED under a widened fence proves the probe could have
# connected all along.
REFUSED, OPEN = "NETFAIL", "CONNECTED"
MATRIX = {
    "real":          {"other-agent": REFUSED, "raven": REFUSED, "bare": REFUSED},
    "removed":       {"other-agent": OPEN,    "raven": OPEN,    "bare": OPEN},
    "widened-agent": {"other-agent": OPEN,    "raven": OPEN,    "bare": REFUSED},
    "widened-raven": {"other-agent": REFUSED, "raven": OPEN,    "bare": REFUSED},
}


def _k(*args, check=True, timeout=300, stdin=None):
    r = subprocess.run(["kubectl", "-n", NS, *args], capture_output=True, text=True,
                       timeout=timeout, check=False, input=stdin)
    if check and r.returncode:
        raise AssertionError(f"kubectl {args} failed: {r.stderr}")
    return r


def _hermes_image() -> str:
    return _k("get", "deploy", DONOR, "-o",
              "jsonpath={.spec.template.spec.containers[0].image}").stdout.strip()


def _cleanup():
    _k("delete", "deploy,svc,pvc", "-l", f"agent.enterprise-ai/user={USER}",
       "--ignore-not-found", "--wait=true", check=False)
    _k("delete", "cm", f"{OBJ}-config", "--ignore-not-found", check=False)
    _k("delete", "secret", f"{OBJ}-key", "--ignore-not-found", check=False)
    _k("delete", "pod", *PROBES.values(), "--ignore-not-found", "--wait=true", check=False)
    _k("delete", "netpol", FENCE, EGRESS, "--ignore-not-found", check=False)


def _render() -> str:
    text = (REPO / "deploy/k8s/65-agent-hermes.template.yaml").read_text()
    for k, v in (("__USER__", USER), ("__NAME__", NAME), ("__IMAGE__", _hermes_image()),
                 ("__MODEL_SOURCE__", "integrated"), ("__KEY_SECRET__", f"{OBJ}-key"),
                 ("__CFGSUM__", "x"), ("__KEYSUM__", "x"), ("__EMAILSUM__", "none"),
                 ("__SLACKSUM__", "none"), ("__DISCORDSUM__", "none")):
        text = text.replace(k, v)
    # The component label moves off `agent` so no shared policy (63/66/68) selects the target.
    text = text.replace("app.kubernetes.io/component: agent\n",
                        f"app.kubernetes.io/component: {TARGET_COMPONENT}\n")
    # Only the resource REQUESTS are shrunk (the shared node is CPU-packed while other work
    # runs); the ports, env and Service under test are untouched.
    return text.replace('requests: { cpu: "500m", memory: "1Gi"', 'requests: { cpu: "20m", memory: "512Mi"')


def _fence_manifest(mode: str) -> dict:
    """The REAL 66 policy, read from the file, as a throwaway copy aimed only at the target.

    'real' keeps the from-selector exactly as shipped; the widened modes rewrite it.
    """
    doc = yaml.safe_load((REPO / "deploy/k8s/66-agent-console-common.yaml").read_text())
    assert doc["kind"] == "NetworkPolicy" and doc["metadata"]["name"] == "agent-console-isolation"
    doc["metadata"] = {"name": FENCE}
    doc["spec"]["podSelector"] = {"matchLabels": {"app.kubernetes.io/component": TARGET_COMPONENT}}
    rule = doc["spec"]["ingress"][0]
    assert any(p["port"] == 8642 for p in rule["ports"]), "66 no longer admits :8642"
    if mode == "widened-agent":
        rule["from"] = [{"podSelector": {"matchLabels": {"app.kubernetes.io/component": "agent"}}}]
    elif mode == "widened-raven":
        rule["from"] = [{"podSelector": {"matchLabels": {"agent.enterprise-ai/type": "raven"}}}]
    else:
        assert mode == "real"
    return doc


def _egress_manifest() -> dict:
    """Throwaway: opens the probes' egress to the target's :8642, additively over any shared
    egress policy that selects them, so the source side is never what refuses them."""
    return {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
            "metadata": {"name": EGRESS},
            "spec": {"podSelector": {"matchLabels": {PROBE_LABEL: "1"}}, "policyTypes": ["Egress"],
                     "egress": [{"to": [{"podSelector": {"matchLabels": {
                         "app.kubernetes.io/component": TARGET_COMPONENT}}}],
                         "ports": [{"protocol": "TCP", "port": 8642}]}]}}


def _set_fence(mode: str) -> None:
    if mode == "removed":
        _k("delete", "netpol", FENCE, "--ignore-not-found")
    else:
        _k("apply", "-f", "-", stdin=json.dumps(_fence_manifest(mode)))


def _probe(src_pod: str, container: str | None, host: str, key: str | None, timeout=8) -> str:
    """POST /v1/chat/completions from inside src_pod. 'HTTP <status> <body>' or 'NETFAIL ...'."""
    auth = f",'Authorization':'Bearer {key}'" if key else ""
    code = (
        "import urllib.request,urllib.error,json\n"
        f"req=urllib.request.Request('http://{host}:8642/v1/chat/completions',"
        "data=json.dumps({'model':'hermes-agent','messages':[{'role':'user','content':'Reply with the single word: pong'}]}).encode(),"
        f"headers={{'Content-Type':'application/json'{auth}}})\n"
        "try:\n"
        f" r=urllib.request.urlopen(req,timeout={timeout});print('HTTP',r.status,r.read().decode()[:4000])\n"
        "except urllib.error.HTTPError as e: print('HTTP',e.code,e.read().decode()[:200])\n"
        "except Exception as e: print('NETFAIL',type(e).__name__,e)\n"
    )
    args = ["exec", src_pod] + (["-c", container] if container else []) + ["--", "python3", "-c", code]
    return _k(*args, check=False, timeout=timeout * 15 + 60).stdout.strip()


def _connect(src_pod: str, container: str | None, host: str, timeout=8) -> str:
    """TCP connect to :8642 from inside src_pod. 'CONNECTED' or 'NETFAIL <why>'."""
    code = (
        "import socket\n"
        "try:\n"
        f" socket.create_connection(('{host}',8642),timeout={timeout}).close();print('CONNECTED')\n"
        "except Exception as e: print('NETFAIL',type(e).__name__,e)\n"
    )
    args = ["exec", src_pod] + (["-c", container] if container else []) + ["--", "python3", "-c", code]
    return _k(*args, check=False, timeout=timeout * 4 + 60).stdout.strip()


def _matrix_now(ip: str) -> dict:
    return {name: (_connect(pod, None, ip).split() or ["?"])[0] for name, pod in PROBES.items()}


def _settle(mode: str, ip: str) -> dict:
    """The CNI syncs policy asynchronously: poll until the observed matrix equals the expected
    one and holds, or time out and return what was seen so the assertion shows it."""
    deadline, seen = time.time() + 90, {}
    while time.time() < deadline:
        seen = _matrix_now(ip)
        if seen == MATRIX[mode]:
            time.sleep(5)
            seen = _matrix_now(ip)
            if seen == MATRIX[mode]:
                return seen
        time.sleep(3)
    return seen


@pytest.fixture(scope="module")
def agent():
    _cleanup()
    donor_cm = json.loads(_k("get", "cm", f"{DONOR}-config", "-o", "json").stdout)
    donor_key = base64.b64decode(json.loads(
        _k("get", "secret", f"{DONOR}-key", "-o", "json").stdout)["data"]["OPENAI_API_KEY"]).decode()
    api_key = "swtestd95-" + os.urandom(16).hex()
    labels = {"agent.enterprise-ai/user": USER, "agent.enterprise-ai/name": NAME}
    objs = [
        {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": f"{OBJ}-key", "labels": labels},
         "stringData": {"OPENAI_API_KEY": donor_key, "DASHBOARD_USERNAME": "console",
                        "DASHBOARD_PASSWORD": "swtestd95-console-password", "API_SERVER_KEY": api_key}},
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": f"{OBJ}-config", "labels": labels},
         # The donor's key is a freerouter key (GATEWAY_PROVIDER=freerouter on this cluster),
         # so point the probe at freerouter and a model it actually serves.
         "data": {"config.yaml": donor_cm["data"]["config.yaml"]
                  .replace("http://gateway:4000/v1", "http://freerouter:8080/v1")
                  .replace("zai-org/GLM-5.3-Flash", "google/gemma-3-27b-it")}},
    ]
    try:
        # Manifests go to kubectl on stdin: no secret material touches disk in this test.
        _k("apply", "-f", "-", stdin=json.dumps({"apiVersion": "v1", "kind": "List", "items": objs}))
        _set_fence("real")
        _k("apply", "-f", "-", stdin=json.dumps(_egress_manifest()))
        _k("apply", "-f", "-", stdin=_render())
        _k("rollout", "status", f"deploy/{OBJ}", "--timeout=300s")
        common = ["--image", _hermes_image(), "--restart=Never", "--command", "--", "sleep", "1800"]

        def run(name, extra):
            _k("run", name, "--labels", PROBE_LABEL + "=1" + (f",{extra}" if extra else ""), *common)

        run(OTHERAGENT, f"{AGENT_LBL},agent.enterprise-ai/user=other{USER},agent.enterprise-ai/name=other")
        run(RAVEN, f"{AGENT_LBL},agent.enterprise-ai/type=raven,agent.enterprise-ai/user={USER}")
        run(BARE, "")
        _k("wait", "--for=condition=Ready", *[f"pod/{p}" for p in PROBES.values()], "--timeout=180s")
        ip = _k("get", "svc", OBJ, "-o", "jsonpath={.spec.clusterIP}").stdout.strip()
        cp = _k("get", "pod", "-l", "app=control-plane", "-o", "jsonpath={.items[0].metadata.name}").stdout.strip()
        deadline = time.time() + 240  # the api server binds after the gateway boots
        while time.time() < deadline:
            if "HTTP 200" in _probe(cp, "control-plane", ip, api_key, timeout=60):
                break
            time.sleep(5)
        yield {"ip": ip, "key": api_key, "cp": cp}
    finally:
        _cleanup()


def test_control_plane_can_open_a_connection(agent):
    assert _connect(agent["cp"], "control-plane", agent["ip"]) == "CONNECTED"


def test_control_plane_gets_a_reply(agent):
    out = _probe(agent["cp"], "control-plane", agent["ip"], agent["key"], timeout=120)
    assert out.startswith("HTTP 200 "), out
    body = json.loads(out[len("HTTP 200 "):])
    assert body["choices"][0]["message"]["content"].strip(), out


def test_api_server_demands_its_own_secret(agent):
    assert _probe(agent["cp"], "control-plane", agent["ip"], None).startswith("HTTP 401")
    assert _probe(agent["cp"], "control-plane", agent["ip"], "wrong-" + agent["key"]).startswith("HTTP 401")


@pytest.mark.parametrize("mode", ["real", "removed", "widened-agent", "widened-raven"])
def test_fence_decides_every_probe(agent, mode):
    """With every probe's egress open, the refusals are the destination fence and only it.

    'real' is the shipped fence: an agent-labelled pod, a Raven-labelled pod and a bare pod
    are all refused. The other rows mutate the fence through its policy data (delete it;
    widen its from-selector to component=agent; widen it to type=raven) and the matrix must
    change exactly as MATRIX says. A fence test that stayed green under the widened rows
    could not tell the shipped fence from a leaky one. A bare TCP connect, not a chat
    request: a permitted chat can outlast a short timeout and look 'refused'.
    """
    try:
        _set_fence(mode)
        seen = _settle(mode, agent["ip"])
    finally:
        _set_fence("real")
    assert seen == MATRIX[mode], f"fence={mode}: expected {MATRIX[mode]}, measured {seen}"


def test_shipped_fence_restored_and_control_plane_still_admitted(agent):
    seen = _settle("real", agent["ip"])
    assert seen == MATRIX["real"], seen
    assert _connect(agent["cp"], "control-plane", agent["ip"]) == "CONNECTED"
