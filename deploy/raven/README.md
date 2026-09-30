# Hosted-mode Raven image

Two images, both built on the cluster with `deploy/bin/kaniko-build.sh` (no local docker):

```
# 1. upstream Raven at a PINNED tag+commit, its own Dockerfile, no edits (remote context;
#    RAVEN_OFFICE=0 drops LibreOffice, ~500MB, which a hosted agent does not need)
deploy/bin/kaniko-build.sh \
  "git://github.com/EverMind-AI/Raven.git#refs/tags/v0.2.3#a9765e55fd1bef51880ea8d96c6e4eb2b40a7e7d" \
  $RAIL_REGISTRY/raven-upstream:v0.2.3 --build-arg RAVEN_OFFICE=0

# 2. the EAF hosted layer (this directory)
deploy/bin/kaniko-build.sh deploy/raven $RAIL_REGISTRY/raven-hosted:v0.2.3-eaf2 \
  --build-arg RAVEN_BASE=$RAIL_REGISTRY/raven-upstream:v0.2.3
```

`KANIKO_CPU_REQUEST=100m` lowers the build pod's CPU request on a crowded node.

Pod contract (what `69-agent-raven.template.yaml` must supply): `AGENT_GATEWAY_BASE`,
`OPENAI_API_KEY` (the Raven's own integrated key), `RAVEN_MODEL`, `RAVEN_SERVE_TOKEN` (pins the
console token the proxy presents as `X-Raven-Token`), `EVEROS_LLM__{BASE_URL,MODEL,API_KEY}` and
`EVEROS_EMBEDDING__{BASE_URL,MODEL,API_KEY}` (all gateway), a PVC on `/data`, and the labels
`component: agent` + `agent.enterprise-ai/type: raven` (so `68-raven-common.yaml` governs it and
`63-agent-common.yaml` does not).

With manager power on, the pod also gets `EAF_AGENT_MANAGER_TOKEN` (Secret) and
`EAF_AGENT_MANAGER_URL` (ConfigMap), and `hosted_seed.py` registers the **eaf-agents** stdio MCP
tool (`eaf_agents_mcp.py`: `list_agents`, `create_agent`, `send_to_agent`). It reaches the
owner's agents only through the control plane: the agent-manager API and the agent relay
(agents-raven.md, Contracts F/G). The token is read from the container env at call time and
never written into `config.json`. Raven asks the owner to approve each call (its default for an
MCP tool).

Proof: `tests/test_raven_hosted_config.py` (hermetic) and `tests-live/test_raven_hosted.py`
(starts the real image on k3s, throwaway `--39e` resources).
