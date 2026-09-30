"""The throwaway `-82e` stack the live voice proof runs against (enterpriseaiframework-82e).

Everything here is namespaced `-82e` and torn down by `teardown()`. The shared cluster is never
touched: the shared control plane, gateway, freerouter, LiveKit and speech deployments are neither
replaced nor restarted. What runs, and where it comes from:

  postgres-82e, valkey-82e   scratch state for the throwaway gateway and control plane
  gateway-82e                the OSS profile's LiteLLM, with the catalogue COPIED from the shared
                             `gateway-config` plus the two audio routes of bundle/litellm/config.base.yaml
  speech-82e                 deploy/k8s/32-speech.yaml (Speaches: faster-whisper STT, Kokoro TTS), renamed
  livekit-82e                the image and shape of deploy/k8s/72-livekit.yaml on its own NodePorts
  voice-worker-82e           deploy/k8s/73-voice-worker.yaml, renamed, image substituted, ports mapped
  cp-82e                     the branch's control-plane image with the never-ready probe convention
                             (label app=control-plane so the shared Service never routes to it)

Shims that exist only because the stack is throwaway, each named so a reader can tell them from
the shipped manifests:
  * the Raven's model is served through `hosted_vllm/` in the copied catalogue: LiteLLM passes
    /v1/responses (which the Raven speaks) straight to an `openai/` upstream, and the upstream has
    no such route; `hosted_vllm/` makes LiteLLM bridge it to chat completions;
  * a NetworkPolicy admitting the Raven to gateway-82e (the shared agent-isolation policy names
    the shared gateway's label, and using that label would put our pod behind the shared Service).
"""
import json
import os
import re
import subprocess
import time
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
NS = "enterprise-ai"
NODE = "k3s-worker"
NODE_IP = "192.168.2.44"
NODEPORTS = (31780, 31781, 31782)  # signal, rtc tcp, rtc udp; 72-livekit.yaml uses 3078x
MASTER_KEY = "sk-82e-master-throwaway"
PG_URL = "postgresql://eaf:pg82e-not-a-secret@postgres-82e:5432"
# The Raven's model. If the shared gateway catalogue carries it, it is bridged there; otherwise (the
# shared gateway is fake-upstream only since the Forge -> freerouter move) it is served through the
# in-cluster freerouter with the operator tenant key read by the pod from the shared secret
# (never through this process), so the Raven really answers and the spend lands in the THROWAWAY
# gateway's ledger on the Raven's alias. A local llama.cpp Qwen was tried and rejected: its
# /v1/responses cannot take the multi-turn history a Raven sends ("Cannot determine type of 'item'").
RAVEN_MODEL = os.environ.get("VOICE_LIVE_RAVEN_MODEL", "deepseek-v4-flash@deepinfra")
UPSTREAM_BASE = os.environ.get("VOICE_LIVE_UPSTREAM_BASE", "http://freerouter:8080/v1")
USER = "baron"  # a principal the real IdP knows, so the control plane can mint the Raven's key


def kubectl(*args, inp=None, check=True):
    r = subprocess.run(["kubectl", "-n", NS, *args], capture_output=True, text=True, input=inp)
    if check and r.returncode != 0:
        raise AssertionError(f"kubectl {' '.join(args[:4])} failed: {r.stderr[-800:]}")
    return r.stdout


def gateway_config() -> dict:
    """The shared catalogue, plus the audio routes, with the Raven's model bridged."""
    cm = json.loads(kubectl("get", "cm", "gateway-config", "-o", "json"))
    cfg = cm["data"]["config.yaml"]
    speech = (REPO / "bundle/litellm/config.base.yaml").read_text()
    block = speech[speech.index("  - model_name: speech-stt"):speech.index("  # ---- upstream models, generated")]
    block = block.replace("http://speech:8000/v1", "http://speech-82e:8000/v1")
    head = cfg.index("model_list:") + len("model_list:\n")
    cfg = cfg[:head] + block + cfg[head:]
    old = f"model: openai/{RAVEN_MODEL}"
    if cfg.count(old) == 1:
        cfg = cfg.replace(old, f"model: hosted_vllm/{RAVEN_MODEL}")
    else:
        assert f"model_name: {RAVEN_MODEL}\n" not in cfg, f"{RAVEN_MODEL} is in the catalogue in an unexpected shape"
        head = cfg.index("model_list:") + len("model_list:\n")
        cfg = cfg[:head] + (
            f"  - model_name: {RAVEN_MODEL}\n    litellm_params:\n"
            f"      model: hosted_vllm/{RAVEN_MODEL}\n      api_base: {UPSTREAM_BASE}\n"
            "      api_key: os.environ/FREEROUTER_MASTER_KEY\n"
            "      input_cost_per_token: 0.0000003\n      output_cost_per_token: 0.0000015\n\n") + cfg[head:]
    cm["data"]["config.yaml"] = cfg
    return {"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": "gateway-config-82e", "namespace": NS}, "data": cm["data"]}


def _rename_speech(text: str) -> str:
    text = re.sub(r"\bapp: speech-models-fetch\b", "app: speech-82e-models-fetch", text)
    text = re.sub(r"\bapp: speech\b", "app: speech-82e", text)
    text = re.sub(r"\bapp: gateway\b", "app: gateway-82e", text)
    for old in ("speech-models-fetch", "speech-models", "speech-aliases", "speech-sealed"):
        text = re.sub(rf"(name|claimName): {old}\b", rf"\1: {old}-82e", text)
    return re.sub(r"(  name: )speech\n", r"\1speech-82e\n", text)


def _worker(text: str, image: str) -> str:
    text = text.replace("__VOICE_WORKER_IMAGE__", image)
    text = text.replace("name: voice-worker\n", "name: voice-worker-82e\n")
    text = text.replace("name: voice-worker-isolation\n", "name: voice-worker-isolation-82e\n")
    text = text.replace("matchLabels: { app: voice-worker }", "matchLabels: { app: voice-worker-82e }")
    text = text.replace("labels: { app: voice-worker, app.kubernetes.io/component: voice-worker }",
                        "labels: { app: voice-worker-82e, app.kubernetes.io/component: voice-worker }")
    text = text.replace('"ws://livekit:7880"', '"ws://livekit-82e:7880"')
    text = text.replace('"http://control-plane:8000/voice/v1"', '"http://cp-82e:8000/voice/v1"')
    text = text.replace("port: 30781", f"port: {NODEPORTS[1]}").replace("port: 30782", f"port: {NODEPORTS[2]}")
    for k in ("LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"):
        text = text.replace(f"secretKeyRef: {{ name: enterprise-ai-secrets, key: {k}, optional: true }}",
                            f"secretKeyRef: {{ name: live82e-secrets, key: {k} }}")
    # the shared cluster is CPU-full; a tiny request keeps the throwaway schedulable
    return text.replace('requests: { cpu: "100m", memory: "384Mi" }', 'requests: { cpu: "5m", memory: "256Mi" }')


def manifests(cp_image: str, worker_image: str, registry: str) -> list[dict]:
    sig, tcp, udp = NODEPORTS
    docs: list[str] = [
        _rename_speech((REPO / "deploy/k8s/32-speech.yaml").read_text()),
        _worker((REPO / "deploy/k8s/73-voice-worker.yaml").read_text(), worker_image),
        f"""
apiVersion: v1
kind: Secret
metadata: {{name: live82e-secrets, namespace: {NS}}}
stringData:
  PG_PASSWORD: pg82e-not-a-secret
  LITELLM_MASTER_KEY: {MASTER_KEY}
  LITELLM_SALT_KEY: sk-82e-salt-throwaway
  LIVEKIT_API_KEY: API82evoicekey
  LIVEKIT_API_SECRET: livekit-82e-throwaway-secret-32-characters-long
  VOICE_SESSION_SECRET: voice-session-82e-throwaway-secret-only-cp
---
apiVersion: v1
kind: ConfigMap
metadata: {{name: postgres-init-82e, namespace: {NS}}}
data: {{init.sql: "CREATE DATABASE litellm;"}}
---
apiVersion: v1
kind: Service
metadata: {{name: postgres-82e, namespace: {NS}}}
spec: {{selector: {{app: postgres-82e}}, ports: [{{port: 5432}}]}}
---
apiVersion: apps/v1
kind: Deployment
metadata: {{name: postgres-82e, namespace: {NS}}}
spec:
  replicas: 1
  strategy: {{type: Recreate}}
  selector: {{matchLabels: {{app: postgres-82e}}}}
  template:
    metadata: {{labels: {{app: postgres-82e}}}}
    spec:
      containers:
        - name: pg
          image: postgres:16-alpine
          env:
            - {{name: POSTGRES_DB, value: controlplane}}
            - {{name: POSTGRES_USER, value: eaf}}
            - name: POSTGRES_PASSWORD
              valueFrom: {{secretKeyRef: {{name: live82e-secrets, key: PG_PASSWORD}}}}
          volumeMounts: [{{name: init, mountPath: /docker-entrypoint-initdb.d}}]
          readinessProbe: {{exec: {{command: ["pg_isready", "-U", "eaf"]}}, periodSeconds: 3}}
          resources: {{requests: {{cpu: 10m, memory: 128Mi}}, limits: {{memory: 512Mi}}}}
      volumes: [{{name: init, configMap: {{name: postgres-init-82e}}}}]
---
apiVersion: v1
kind: Service
metadata: {{name: valkey-82e, namespace: {NS}}}
spec: {{selector: {{app: valkey-82e}}, ports: [{{port: 6379}}]}}
---
apiVersion: apps/v1
kind: Deployment
metadata: {{name: valkey-82e, namespace: {NS}}}
spec:
  replicas: 1
  selector: {{matchLabels: {{app: valkey-82e}}}}
  template:
    metadata: {{labels: {{app: valkey-82e}}}}
    spec:
      containers:
        - name: valkey
          image: valkey/valkey:8-alpine
          resources: {{requests: {{cpu: 5m, memory: 32Mi}}, limits: {{memory: 128Mi}}}}
---
apiVersion: v1
kind: Service
metadata: {{name: gateway-82e, namespace: {NS}}}
spec: {{selector: {{app: gateway-82e}}, ports: [{{port: 4000}}]}}
---
apiVersion: apps/v1
kind: Deployment
metadata: {{name: gateway-82e, namespace: {NS}}}
spec:
  replicas: 1
  selector: {{matchLabels: {{app: gateway-82e}}}}
  template:
    metadata: {{labels: {{app: gateway-82e}}}}
    spec:
      containers:
        - name: litellm
          image: ghcr.io/berriai/litellm:main-v1.77.3-stable
          args: ["--config", "/app/config.yaml", "--port", "4000"]
          env:
            - name: LITELLM_MASTER_KEY
              valueFrom: {{secretKeyRef: {{name: live82e-secrets, key: LITELLM_MASTER_KEY}}}}
            - name: LITELLM_SALT_KEY
              valueFrom: {{secretKeyRef: {{name: live82e-secrets, key: LITELLM_SALT_KEY}}}}
            - {{name: DATABASE_URL, value: "{PG_URL}/litellm"}}
            - {{name: REDIS_URL, value: "redis://valkey-82e:6379"}}
            - name: FREEROUTER_MASTER_KEY
              valueFrom: {{secretKeyRef: {{name: enterprise-ai-secrets, key: FREEROUTER_MASTER_KEY, optional: true}}}}
            - name: FORGE_API_KEY
              valueFrom: {{secretKeyRef: {{name: enterprise-ai-secrets, key: FORGE_API_KEY, optional: true}}}}
          ports: [{{containerPort: 4000}}]
          readinessProbe:
            httpGet: {{path: /health/liveliness, port: 4000}}
            initialDelaySeconds: 20
            periodSeconds: 10
            failureThreshold: 30
          volumeMounts:
{''.join(f'''            - {{mountPath: /app/{f}, name: config, readOnly: true, subPath: {f}}}
''' for f in ("config.yaml", "strip_reasoning.py", "require_principal.py",
              "flush_spend_on_shutdown.py", "allow_reasoning_effort.py"))}          resources: {{requests: {{cpu: 30m, memory: 512Mi}}, limits: {{memory: 2Gi}}}}
      volumes: [{{name: config, configMap: {{name: gateway-config-82e}}}}]
---
apiVersion: v1
kind: Service
metadata: {{name: livekit-82e, namespace: {NS}}}
spec:
  type: NodePort
  externalTrafficPolicy: Local
  selector: {{run: livekit-82e}}
  ports:
    - {{name: signal, protocol: TCP, port: 7880, targetPort: 7880, nodePort: {sig}}}
    - {{name: rtc-tcp, protocol: TCP, port: {tcp}, targetPort: {tcp}, nodePort: {tcp}}}
    - {{name: rtc-udp, protocol: UDP, port: {udp}, targetPort: {udp}, nodePort: {udp}}}
---
apiVersion: apps/v1
kind: Deployment
metadata: {{name: livekit-82e, namespace: {NS}}}
spec:
  replicas: 1
  strategy: {{type: Recreate}}
  selector: {{matchLabels: {{run: livekit-82e}}}}
  template:
    # `app: livekit` so the UNMODIFIED voice-worker NetworkPolicy peer matches; `run` keeps the
    # throwaway Service from selecting anything else.
    metadata: {{labels: {{app: livekit, run: livekit-82e}}}}
    spec:
      enableServiceLinks: false  # as 72-livekit.yaml: a `livekit` Service would inject LIVEKIT_PORT
      nodeSelector: {{kubernetes.io/hostname: {NODE}}}
      automountServiceAccountToken: false
      containers:
        - name: livekit
          image: {_livekit_image()}
          args: ["--config-body", "$(LIVEKIT_CONFIG_BODY)"]
          env:
            - name: NODE_IP
              valueFrom: {{fieldRef: {{fieldPath: status.hostIP}}}}
            - name: LIVEKIT_API_KEY
              valueFrom: {{secretKeyRef: {{name: live82e-secrets, key: LIVEKIT_API_KEY}}}}
            - name: LIVEKIT_API_SECRET
              valueFrom: {{secretKeyRef: {{name: live82e-secrets, key: LIVEKIT_API_SECRET}}}}
            - name: LIVEKIT_CONFIG_BODY
              value: |
                port: 7880
                bind_addresses: [""]
                rtc:
                  tcp_port: {tcp}
                  udp_port: {udp}
                  node_ip: $(NODE_IP)
                  use_external_ip: false
                turn:
                  enabled: false
                keys:
                  $(LIVEKIT_API_KEY): $(LIVEKIT_API_SECRET)
          ports:
            - {{containerPort: 7880, protocol: TCP}}
            - {{containerPort: {tcp}, protocol: TCP}}
            - {{containerPort: {udp}, protocol: UDP}}
          resources: {{requests: {{cpu: 10m, memory: 128Mi}}, limits: {{memory: 512Mi}}}}
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata: {{name: raven-egress-gateway-82e, namespace: {NS}}}
spec:
  podSelector: {{matchLabels: {{agent.enterprise-ai/name: r82e}}}}
  policyTypes: [Egress]
  egress:
    - to: [{{podSelector: {{matchLabels: {{app: gateway-82e}}}}}}]
      ports: [{{protocol: TCP, port: 4000}}]
---
apiVersion: v1
kind: Service
metadata: {{name: cp-82e, namespace: {NS}}}
spec:
  publishNotReadyAddresses: true
  selector: {{run: cp-82e}}
  ports: [{{name: http, port: 8000, targetPort: 8000}}]
---
apiVersion: v1
kind: Pod
metadata:
  name: cp-82e
  namespace: {NS}
  labels: {{app: control-plane, run: cp-82e}}
spec:
  serviceAccountName: control-plane
  containers:
    - name: control-plane
      image: {cp_image}
      imagePullPolicy: Always
      ports: [{{containerPort: 8000}}]
      readinessProbe: {{exec: {{command: ["false"]}}, periodSeconds: 10}}
      env:
        - {{name: CONTROL_PLANE_DATABASE_URL, value: "{PG_URL}/controlplane"}}
        - {{name: GATEWAY_DATABASE_URL, value: "{PG_URL}/litellm"}}
        - {{name: GATEWAY_URL, value: "http://gateway-82e:4000"}}
        - name: GATEWAY_MASTER_KEY
          valueFrom: {{secretKeyRef: {{name: live82e-secrets, key: LITELLM_MASTER_KEY}}}}
        - {{name: GATEWAY_PROVIDER, value: litellm}}
        - {{name: IDP_URL, value: "http://identity:8080"}}
        - {{name: IDP_REALM, value: enterprise-ai}}
        - {{name: IDP_CLIENT_ID, value: control-plane}}
        - name: IDP_CLIENT_SECRET
          valueFrom: {{secretKeyRef: {{name: enterprise-ai-secrets, key: IDP_CLIENT_SECRET}}}}
        - name: CONTROL_PLANE_ADMIN_TOKEN
          valueFrom: {{secretKeyRef: {{name: enterprise-ai-secrets, key: CONTROL_PLANE_ADMIN_TOKEN}}}}
        - name: PUBLIC_BASE_URL
          valueFrom: {{secretKeyRef: {{name: enterprise-ai-secrets, key: PUBLIC_BASE_URL}}}}
        - {{name: PORTAL_ADMINS, value: {USER}}}
        - {{name: AGENT_MODELS, value: "{RAVEN_MODEL}"}}
        - {{name: AGENT_MODEL, value: "{RAVEN_MODEL}"}}
        - {{name: CATALOG_URL, value: "http://127.0.0.1:1"}}
        - {{name: AGENT_GATEWAY_BASE, value: "http://gateway-82e:4000/v1"}}
        - {{name: AGENT_RAVEN_IMAGE, value: "{registry}/raven-hosted:v0.2.3-eaf2"}}
        - name: LIVEKIT_API_KEY
          valueFrom: {{secretKeyRef: {{name: live82e-secrets, key: LIVEKIT_API_KEY}}}}
        - name: LIVEKIT_API_SECRET
          valueFrom: {{secretKeyRef: {{name: live82e-secrets, key: LIVEKIT_API_SECRET}}}}
        - name: VOICE_SESSION_SECRET
          valueFrom: {{secretKeyRef: {{name: live82e-secrets, key: VOICE_SESSION_SECRET}}}}
        - {{name: LIVEKIT_API_URL, value: "http://livekit-82e:7880"}}
        - {{name: LIVEKIT_URL, value: "ws://{NODE_IP}:{sig}"}}
      volumeMounts: [{{name: agent-assets, mountPath: /etc/agent-assets, readOnly: true}}]
      resources: {{requests: {{cpu: 20m, memory: 128Mi}}, limits: {{memory: 512Mi}}}}
  volumes: [{{name: agent-assets, configMap: {{name: agent-assets-82e}}}}]
""",
    ]
    out = []
    for d in docs:
        out += [x for x in yaml.safe_load_all(d) if x]
    return out


def _livekit_image() -> str:
    for d in yaml.safe_load_all((REPO / "deploy/k8s/72-livekit.yaml").read_text()):
        if d and d["kind"] == "Deployment":
            return d["spec"]["template"]["spec"]["containers"][0]["image"]
    raise AssertionError("no livekit Deployment in 72-livekit.yaml")


ASSET_FILES = {
    "64-agent.template.yaml": "deploy/k8s/64-agent.template.yaml",
    "65-agent-hermes.template.yaml": "deploy/k8s/65-agent-hermes.template.yaml",
    "67-agent-openclaw.template.yaml": "deploy/k8s/67-agent-openclaw.template.yaml",
    "69-agent-raven.template.yaml": "deploy/k8s/69-agent-raven.template.yaml",
    "entrypoint.sh": "deploy/agent/entrypoint.sh", "agent-email": "deploy/agent/agent-email",
    "EMAIL.md": "deploy/agent/EMAIL.md", "agent-slack": "deploy/agent/agent-slack",
    "SLACK.md": "deploy/agent/SLACK.md", "agent-discord": "deploy/agent/agent-discord",
    "DISCORD.md": "deploy/agent/DISCORD.md", "agentws.py": "deploy/agent/agentws.py",
}


def apply(cp_image: str, worker_image: str, registry: str) -> None:
    args = ["create", "configmap", "agent-assets-82e", "--dry-run=client", "-o", "yaml"]
    for name, path in ASSET_FILES.items():
        args += [f"--from-file={name}={REPO / path}"]
    kubectl("apply", "-f", "-", inp=kubectl(*args))
    kubectl("apply", "-f", "-", inp=json.dumps(gateway_config()))
    kubectl("apply", "-f", "-", inp=yaml.safe_dump_all(manifests(cp_image, worker_image, registry)))


def teardown() -> None:
    """Every object this stack (and the Raven it creates) made, by the -82e names, and it WAITS:
    a PVC still Terminating from the last run would swallow the next apply and leave a pod
    Pending for good (found the hard way: the first live run timed out on exactly that)."""
    workloads = (
        ("pod", ["cp-82e"]),
        ("deploy", ["voice-worker-82e", "speech-82e", "gateway-82e", "livekit-82e", "postgres-82e",
                    "valkey-82e", f"agent-{USER}-r82e"]),
        ("job", ["speech-models-82e-fetch-82e"]),
    )
    rest = (
        ("svc", ["cp-82e", "speech-82e", "gateway-82e", "livekit-82e", "postgres-82e", "valkey-82e",
                 f"agent-{USER}-r82e"]),
        ("cm", ["gateway-config-82e", "agent-assets-82e", "postgres-init-82e", "speech-aliases-82e",
                f"agent-{USER}-r82e-config"]),
        ("secret", ["live82e-secrets", f"agent-{USER}-r82e-key"]),
        ("networkpolicy", ["voice-worker-isolation-82e", "speech-sealed-82e", "raven-egress-gateway-82e"]),
        ("pvc", ["speech-models-82e", f"agent-{USER}-r82e"]),
    )
    for kind, names in workloads + rest:
        kubectl("delete", kind, *names, "--ignore-not-found", "--wait=true", "--timeout=180s", check=False)
    deadline = time.time() + 180
    while time.time() < deadline:
        left = [f"{k}/{n}" for k, names in workloads + rest for n in names
                if kubectl("get", k, n, "-o", "name", check=False).strip()]
        if not left:
            return
        time.sleep(3)
    raise AssertionError(f"teardown left objects behind: {left}")
