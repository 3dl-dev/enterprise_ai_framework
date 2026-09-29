"""LIVE: the hosted-mode Raven image really starts on k3s and really cannot reach EverMind.

Item enterpriseaiframework-39e (agents-raven.md Contract E + the EAF owner's review).
Nothing here mocks the container start or the network. The test deploys a throwaway Raven
(names carry the `--39e` suffix and are removed in teardown) from the pushed hosted image and:

  * waits for the pod to be Ready (readiness = the WebUI answering on :18793),
  * fetches the WebUI from the control-plane pod to the Raven pod IP :18793,
  * proves another pod CANNOT reach :18793 (the proxy is the only door),
  * inspects the running container: Curator/Evolver absent, hosted config.json, baked env,
    EverOS's own process environment pointing at the gateway, EverOS state on the PVC,
  * attempts real connections OUT of the pod to EverMind hosts and the open internet and
    requires them to fail, with a positive control pod (no policy) that reaches the same
    hosts, so a failure is attributable to the NetworkPolicy and not to a dead network.

The NetworkPolicy under test is deploy/k8s/68-raven-common.yaml itself, with only its
podSelector rewritten to this throwaway's label (the shared 63-agent-common.yaml, which
grants internet egress to every agent, is deliberately not touched on the shared cluster).

Requires kubectl on the dev cluster and RAVEN_HOSTED_IMAGE (default: the registry tag below).
"""

import copy
import json
import os
import secrets
import subprocess
import time
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
NS = "enterprise-ai"
SUFFIX = "39e"
NAME = f"raven-{SUFFIX}"
CTL = f"raven-ctl-{SUFFIX}"
LABEL = {"eaf-raven-live-test": SUFFIX}
IMAGE = os.environ.get("RAVEN_HOSTED_IMAGE", "192.168.2.43:30500/raven-hosted:v0.2.3-eaf2")
GATEWAY = "http://gateway:4000/v1"
DUMMY_KEY = "sk-eaf-39e-not-a-real-key"  # throwaway: the boot path never spends
EVERMIND_HOSTS = ["skillhub.evermind.ai", "raven.evermind.ai", "api.github.com"]


def kubectl(*args, input=None, check=True, timeout=120):
    r = subprocess.run(["kubectl", "-n", NS, *args], input=input, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise AssertionError(f"kubectl {' '.join(args)} failed: {r.stderr.strip()[:800]}")
    return r


def _apply(objs):
    kubectl("apply", "-f", "-", input=yaml.safe_dump_all(objs))


def _isolation_policy():
    doc = yaml.safe_load((REPO / "deploy" / "k8s" / "68-raven-common.yaml").read_text())
    doc = copy.deepcopy(doc)
    doc["metadata"]["name"] = f"raven-isolation-{SUFFIX}"
    doc["spec"]["podSelector"] = {"matchLabels": LABEL}
    return doc


def _manifests(token):
    meta = lambda n, extra=None: {"name": n, "namespace": NS, "labels": {**LABEL, "app": n, **(extra or {})}}  # noqa: E731
    pvc = {
        "apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": meta(f"{NAME}-data"),
        "spec": {"accessModes": ["ReadWriteOnce"], "storageClassName": "local-path",
                 "resources": {"requests": {"storage": "1Gi"}}},
    }
    secret = {
        "apiVersion": "v1", "kind": "Secret", "metadata": meta(f"{NAME}-key"), "type": "Opaque",
        "stringData": {"OPENAI_API_KEY": DUMMY_KEY, "RAVEN_SERVE_TOKEN": token},
    }
    sec = lambda k: {"secretKeyRef": {"name": f"{NAME}-key", "key": k}}  # noqa: E731
    env = [
        {"name": "OPENAI_API_KEY", "valueFrom": sec("OPENAI_API_KEY")},
        {"name": "RAVEN_SERVE_TOKEN", "valueFrom": sec("RAVEN_SERVE_TOKEN")},
        {"name": "AGENT_GATEWAY_BASE", "value": GATEWAY},
        {"name": "RAVEN_MODEL", "value": "claude-sonnet-4-5"},
        # EverOS memory inference, all of it, through the gateway on the Raven's own key
        # (design Contract E: [llm] AND the embedding model; no bundled/hosted embedder).
        {"name": "EVEROS_LLM__BASE_URL", "value": GATEWAY},
        {"name": "EVEROS_LLM__MODEL", "value": "claude-sonnet-4-5"},
        {"name": "EVEROS_LLM__API_KEY", "valueFrom": sec("OPENAI_API_KEY")},
        {"name": "EVEROS_EMBEDDING__BASE_URL", "value": GATEWAY},
        {"name": "EVEROS_EMBEDDING__MODEL", "value": "text-embedding-3-small"},
        {"name": "EVEROS_EMBEDDING__API_KEY", "valueFrom": sec("OPENAI_API_KEY")},
    ]
    dep = {
        "apiVersion": "apps/v1", "kind": "Deployment", "metadata": meta(NAME),
        "spec": {
            "replicas": 1, "strategy": {"type": "Recreate"},
            "selector": {"matchLabels": {"app": NAME}},
            "template": {
                "metadata": {"labels": {**LABEL, "app": NAME}},
                "spec": {
                    "automountServiceAccountToken": False,
                    "containers": [{
                        "name": "raven", "image": IMAGE, "imagePullPolicy": "Always", "env": env,
                        "ports": [{"containerPort": 18793}],
                        "volumeMounts": [{"name": "data", "mountPath": "/data"}],
                        # requests are tiny on purpose: the shared worker is ~99% CPU-requested.
                        "resources": {"requests": {"cpu": "2m", "memory": "256Mi"},
                                      "limits": {"cpu": "2", "memory": "3Gi"}},
                        "startupProbe": {"httpGet": {"path": "/health", "port": 18793},
                                         "periodSeconds": 5, "failureThreshold": 90},
                        "readinessProbe": {"httpGet": {"path": "/health", "port": 18793}, "periodSeconds": 10},
                    }],
                    "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": f"{NAME}-data"}}],
                },
            },
        },
    }
    # Positive control: same image, NO isolation policy, carries a different label.
    ctl = {
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"name": CTL, "namespace": NS, "labels": {"app": CTL, "eaf-raven-live-ctl": SUFFIX}},
        "spec": {
            "automountServiceAccountToken": False, "restartPolicy": "Never",
            "containers": [{"name": "c", "image": IMAGE, "imagePullPolicy": "Always",
                            "command": ["sleep", "3600"],
                            "resources": {"requests": {"cpu": "1m", "memory": "16Mi"}}}],
        },
    }
    return [pvc, secret, dep, _isolation_policy(), ctl]


def _pod_name():
    out = kubectl("get", "pods", "-l", f"app={NAME}", "-o", "jsonpath={.items[0].metadata.name}").stdout
    return out.strip()


def _exec(pod, *cmd, check=True, timeout=60):
    return kubectl("exec", pod, "--", *cmd, check=check, timeout=timeout)


def _teardown():
    for kind, name in (("deployment", NAME), ("pod", CTL), ("networkpolicy", f"raven-isolation-{SUFFIX}"),
                       ("secret", f"{NAME}-key"), ("pvc", f"{NAME}-data")):
        kubectl("delete", kind, name, "--ignore-not-found", "--wait=true", "--timeout=180s", check=False, timeout=200)


@pytest.fixture(scope="module")
def raven():
    _teardown()
    token = secrets.token_hex(16)
    try:
        _apply(_manifests(token))
        r = kubectl("rollout", "status", f"deployment/{NAME}", "--timeout=600s", check=False, timeout=630)
        if r.returncode != 0:
            desc = kubectl("describe", "pods", "-l", f"app={NAME}", check=False).stdout[-1500:]
            logs = kubectl("logs", f"deployment/{NAME}", "--tail=60", check=False).stdout[-2500:]
            raise AssertionError(f"raven pod never became Ready\n{r.stdout}{r.stderr}\n{desc}\n{logs}")
        kubectl("wait", "--for=condition=Ready", f"pod/{CTL}", "--timeout=180s")
        pod = _pod_name()
        ip = kubectl("get", "pod", pod, "-o", "jsonpath={.status.podIP}").stdout.strip()
        yield {"pod": pod, "ip": ip, "token": token}
    finally:
        if not os.environ.get("RAVEN_LIVE_KEEP"):  # debugging aid: leave the throwaway up
            _teardown()


def test_pod_is_ready_in_hosted_mode(raven):
    ready = kubectl("get", "pod", raven["pod"], "-o", "jsonpath={.status.conditions[?(@.type=='Ready')].status}").stdout
    assert ready == "True"


def test_webui_reachable_from_control_plane_pod_ip_18793(raven):
    code = (
        "import urllib.request,sys;"
        f"r=urllib.request.urlopen('http://{raven['ip']}:18793/',timeout=15);"
        "b=r.read().decode('utf-8','replace');print(r.status);print(len(b));print(b[:600].lower())"
    )
    out = kubectl("exec", "deploy/control-plane", "-c", "control-plane", "--", "python", "-c", code).stdout.splitlines()
    assert out[0] == "200"
    assert int(out[1]) > 10_000, "the WebUI is one large inlined page; a tiny body is not the page"
    assert "<html" in "\n".join(out[2:]) or "<!doctype html" in "\n".join(out[2:])


def test_no_other_pod_can_reach_the_webui(raven):
    """The proxy is the only door: a same-namespace pod that is not the control plane is refused."""
    r = _exec(CTL, "curl", "-sS", "-m", "8", "-o", "/dev/null", "-w", "%{http_code}", f"http://{raven['ip']}:18793/",
              check=False)
    assert r.returncode != 0, f"a non-control-plane pod reached the WebUI: {r.stdout!r}"


def test_curator_and_evolver_are_absent(raven):
    r = _exec(raven["pod"], "sh", "-c", "ls -d /app/experimental/curator /app/evolver 2>&1; echo rc=$?", check=False)
    assert "No such file" in r.stdout and "rc=2" in r.stdout, r.stdout
    imp = _exec(raven["pod"], "python", "-c", "import importlib.util as u;print(u.find_spec('evolver'))").stdout
    assert imp.strip() == "None"


def test_running_config_and_env_are_hosted(raven):
    cfg = json.loads(_exec(raven["pod"], "cat", "/data/.raven/config.json").stdout)
    assert cfg["providers"]["custom"]["apiBase"] == GATEWAY
    assert cfg["agents"]["defaults"]["provider"] == "custom"
    assert cfg["a2a"]["server"]["enabled"] is False
    assert not [a for a in cfg.get("subagents", {}).get("agents", []) if a.get("kind", "builtin") != "builtin" and a.get("enabled")]
    env = dict(line.split("=", 1) for line in _exec(raven["pod"], "env").stdout.splitlines() if "=" in line)
    assert env["RAVEN_AUTO_LOGIN"] == "0"
    assert env["RAVEN_NO_UPDATE_CHECK"] == "1"
    assert env["RAVEN_HOSTED"] == "1"
    assert "evermind" not in env["RAVEN_SKILLHUB_URL"]


_LISTEN = "lsof -nP -iTCP -sTCP:LISTEN 2>/dev/null | awk 'NR>1{print $9}' | sort -u"


def _listeners(pod):
    return set(_exec(pod, "sh", "-c", _LISTEN).stdout.split())


def _wait_for_everos(pod, timeout=120):
    """nginx (and so Ready) comes up before the memory server; EverOS takes ~10s more."""
    end = time.time() + timeout
    while time.time() < end:
        if "127.0.0.1:18791" in _listeners(pod):
            return
        time.sleep(3)


def test_listeners_engine_on_loopback_webui_on_18793_everos_18791(raven):
    _wait_for_everos(raven["pod"])
    socks = _listeners(raven["pod"])
    assert "*:18793" in socks, socks          # nginx: reachable from the pod IP
    assert "127.0.0.1:18792" in socks, socks  # the engine stays on loopback
    assert "127.0.0.1:18791" in socks, f"EverOS is not listening on :18791: {socks}"
    assert not any(s.startswith("*:") and s != "*:18793" for s in socks), f"unexpected public listener: {socks}"


_SCAN = (
    "import os,json\n"
    "out=[]\n"
    "for p in os.listdir('/proc'):\n"
    "    if not p.isdigit(): continue\n"
    "    try:\n"
    "        cmd=open(f'/proc/{p}/cmdline','rb').read().replace(b'\\0',b' ').decode()\n"
    "        env=dict(kv.split('=',1) for kv in open(f'/proc/{p}/environ','rb').read().decode().split('\\0') if '=' in kv)\n"
    "    except OSError: continue\n"
    "    if 'everos server start' in cmd: out.append({'cmd':cmd,'env':{k:v for k,v in env.items() if k.startswith('EVEROS_')}})\n"
    "print(json.dumps(out))\n"
)


def test_everos_process_environment_points_at_the_gateway_and_state_is_on_the_pvc(raven):
    _wait_for_everos(raven["pod"])
    servers = json.loads(_exec(raven["pod"], "python", "-c", _SCAN).stdout)
    assert servers, "no `everos server start` process is running"
    # Every EverOS server in the pod (the host's and the built-in sub-agent's) must send
    # ALL model inference, embeddings included, to the gateway. None may fall back to a
    # bundled or hosted embedder.
    for srv in servers:
        env = srv["env"]
        assert env["EVEROS_EMBEDDING__BASE_URL"] == GATEWAY, srv
        assert env["EVEROS_LLM__BASE_URL"] == GATEWAY, srv
        assert env["EVEROS_EMBEDDING__API_KEY"] == DUMMY_KEY and env["EVEROS_LLM__API_KEY"] == DUMMY_KEY
    host = next(s for s in servers if "--root /data/.raven/everos" in s["cmd"])
    assert host["cmd"].strip().endswith("/data/.raven/everos")
    # State on the PVC: the EverOS root sits on the same device as /data, and /data is a mount.
    same = _exec(raven["pod"], "sh", "-c",
                 "test $(stat -c %d /data/.raven/everos) = $(stat -c %d /data) && echo same").stdout.strip()
    assert same == "same"
    assert _exec(raven["pod"], "sh", "-c", "grep ' /data ' /proc/mounts").stdout.strip(), "/data is not a mount"


@pytest.mark.parametrize("host", EVERMIND_HOSTS + ["1.1.1.1"])
def test_no_egress_from_the_raven_pod(raven, host):
    url = f"https://{host}/" if host[0].isalpha() else f"https://{host}/"
    r = _exec(raven["pod"], "curl", "-sS", "-m", "8", "-o", "/dev/null", "-w", "%{http_code}", url, check=False)
    assert r.returncode != 0, f"raven pod reached {url}: HTTP {r.stdout}"


@pytest.mark.parametrize("host", EVERMIND_HOSTS + ["1.1.1.1"])
def test_positive_control_same_image_without_the_policy_reaches_out(raven, host):
    """If the control cannot reach the host either, the test above proves nothing about the policy."""
    url = f"https://{host}/"
    r = _exec(CTL, "curl", "-sS", "-m", "15", "-o", "/dev/null", "-w", "%{http_code}", url, check=False)
    assert r.returncode == 0 and r.stdout.strip() not in ("", "000"), f"control could not reach {url}: {r.stderr[:200]}"


def test_the_gateway_route_is_still_open_from_the_raven_pod(raven):
    r = _exec(raven["pod"], "curl", "-sS", "-m", "10", "-o", "/dev/null", "-w", "%{http_code}", "http://gateway:4000/health/liveliness", check=False)
    assert r.returncode == 0 and r.stdout.strip().isdigit() and r.stdout.strip() != "000", (r.stdout, r.stderr)
