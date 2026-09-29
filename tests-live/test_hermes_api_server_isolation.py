"""The hermes API server (:8642) answers the control plane and nobody else (enterpriseaiframework-147).

Run against the live k3s cluster:  pytest tests-live/test_hermes_api_server_isolation.py

Mock-free. It renders the REAL 65-agent-hermes.template.yaml by literal placeholder
substitution, applies it beside the REAL 66-agent-console-common.yaml policy, and measures:

  * from the control-plane pod, POST /v1/chat/completions with the agent's own Bearer secret
    returns a model reply;
  * the same request without (or with a wrong) secret is rejected by hermes: the port has
    its own auth on top of the network fence;
  * from another agent pod, and from a pod wearing the labels a Raven pod will carry, the TCP
    connection to :8642 fails at the network level (no HTTP status at all), even when the
    source presents the valid key. That is the fence, measured.

Expected values come from the cluster (the key is the one this test wrote into the Secret the
agent started with; "refused" is a transport failure observed from the source pod), never from
the code under test. Throwaway objects are `*swtest147*`, removed even on failure. The agent
borrows the model config and virtual key of an existing hermes agent so inference works.
"""
import base64
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

NS = "enterprise-ai"
USER, NAME = "swtest147", "probe"
OBJ = f"agent-{USER}-{NAME}"
REPO = Path(__file__).resolve().parent.parent
DONOR = "agent-baron-chad"  # a live hermes agent: donates model config + a spendable key
RAVEN = "raven-swtest147"
BARE = "bare-swtest147"  # no labels: no egress policy of its own, so ONLY the destination fence can stop it


def _k(*args, check=True, timeout=300):
    r = subprocess.run(["kubectl", "-n", NS, *args], capture_output=True, text=True,
                       timeout=timeout, check=False)
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
    _k("delete", "pod", RAVEN, BARE, "--ignore-not-found", "--wait=true", check=False)


def _render() -> str:
    text = (REPO / "deploy/k8s/65-agent-hermes.template.yaml").read_text()
    for k, v in (("__USER__", USER), ("__NAME__", NAME), ("__IMAGE__", _hermes_image()),
                 ("__MODEL_SOURCE__", "integrated"), ("__KEY_SECRET__", f"{OBJ}-key"),
                 ("__CFGSUM__", "x"), ("__KEYSUM__", "x"), ("__EMAILSUM__", "none"),
                 ("__SLACKSUM__", "none"), ("__DISCORDSUM__", "none")):
        text = text.replace(k, v)
    # Only the resource REQUESTS are shrunk (the shared node is CPU-packed while other work
    # runs); the ports, env, Service and policy under test are untouched.
    return text.replace('requests: { cpu: "500m", memory: "1Gi"', 'requests: { cpu: "20m", memory: "512Mi"')


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


@pytest.fixture(scope="module")
def agent():
    _cleanup()
    donor_cm = json.loads(_k("get", "cm", f"{DONOR}-config", "-o", "json").stdout)
    donor_key = base64.b64decode(json.loads(
        _k("get", "secret", f"{DONOR}-key", "-o", "json").stdout)["data"]["OPENAI_API_KEY"]).decode()
    api_key = "swtest147-" + os.urandom(16).hex()
    labels = {"agent.enterprise-ai/user": USER, "agent.enterprise-ai/name": NAME}
    objs = [
        {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": f"{OBJ}-key", "labels": labels},
         "stringData": {"OPENAI_API_KEY": donor_key, "DASHBOARD_USERNAME": "console",
                        "DASHBOARD_PASSWORD": "swtest147-console-password", "API_SERVER_KEY": api_key}},
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": f"{OBJ}-config", "labels": labels},
         # The donor's key is a freerouter key (GATEWAY_PROVIDER=freerouter on this cluster),
         # so point the probe at freerouter and a model it actually serves.
         "data": {"config.yaml": donor_cm["data"]["config.yaml"]
                  .replace("http://gateway:4000/v1", "http://freerouter:8080/v1")
                  .replace("zai-org/GLM-5.3-Flash", "google/gemma-3-27b-it")}},
    ]
    try:
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "pre.json").write_text(json.dumps({"apiVersion": "v1", "kind": "List", "items": objs}))
            (Path(d) / "agent.yaml").write_text(_render())
            _k("apply", "-f", str(Path(d) / "pre.json"))
            _k("apply", "-f", str(REPO / "deploy/k8s/66-agent-console-common.yaml"))
            _k("apply", "-f", str(Path(d) / "agent.yaml"))
        _k("rollout", "status", f"deploy/{OBJ}", "--timeout=300s")
        # a pod wearing what a Raven pod will wear (agents-raven.md Contract E)
        _k("run", RAVEN, "--image", _hermes_image(), "--restart=Never",
           "--labels", "app.kubernetes.io/component=agent,agent.enterprise-ai/type=raven,"
                       f"agent.enterprise-ai/user={USER}", "--command", "--", "sleep", "900")
        _k("run", BARE, "--image", _hermes_image(), "--restart=Never", "--command", "--", "sleep", "900")
        _k("wait", "--for=condition=Ready", f"pod/{RAVEN}", f"pod/{BARE}", "--timeout=180s")
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


@pytest.mark.parametrize("src", ["other-agent", "raven", "bare"])
def test_connection_from_other_pods_is_refused(agent, src):
    if src == "other-agent":
        pod = _k("get", "pod", "-l", "agent.enterprise-ai/name=chad,agent.enterprise-ai/user=baron",
                 "-o", "jsonpath={.items[0].metadata.name}").stdout.strip()
        container = "agent"
    else:
        pod, container = (RAVEN if src == "raven" else BARE), None
    out = _connect(pod, container, agent["ip"])
    # A bare TCP connect, not a chat request: a permitted chat can outlast a short timeout and
    # would then look "refused". ("bare" carries no policy of its own, so it isolates the
    # destination's INGRESS fence from the agent egress rules.)
    assert out.startswith("NETFAIL"), f"{src} can open a TCP connection to :8642 -> {out}"
