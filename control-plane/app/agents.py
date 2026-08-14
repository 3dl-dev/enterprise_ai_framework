"""The agent lifecycle, driven from the control plane over the Kubernetes API.

WHAT THIS IS

Contract 2 of docs/design/records/agents-surface.md names four states — created, running,
stopped, deleted — and one concrete Kubernetes action for each transition. Until now the
only thing that could drive them was `deploy/bin/provision-agent.sh`, which is an operator
script on an operator's laptop with an operator's kubeconfig. A user cannot run it, so a
user could not have an agent. This module is the same four transitions, executed by the
control-plane pod on behalf of an authenticated person.

WHY httpx AND NOT kubectl

The control-plane image is `python:3.12-slim` plus a requirements file. There is no kubectl
in it and adding one would mean shipping a 50MB binary to shell out to, for calls that are
four HTTP verbs against an API we already talk to (`app/agent_usage.py` has been reading
pods this way since -914). So the credential helpers are imported from that module rather
than re-derived: one place reads the projected service-account token, one place decides
the TLS trust, and a token rotation cannot fix one caller and break the other.

WHY THE OWNER IS NEVER A PARAMETER, AND WHY THAT IS NOT ENOUGH ON ITS OWN

Every function here takes `user` from `portal.require_user()` and builds the object name as
`agent-<user>-<name>`. That is the same discipline `/portal/api/keys/rotate` uses: there is
no argument that can be pointed at somebody else.

It is necessary and NOT sufficient, because the two segments are joined by a hyphen and
both may contain hyphens:

    user "alice"   + name "bot-two"  -> agent-alice-bot-two
    user "alice-bot" + name "two"    -> agent-alice-bot-two

Two different people, one object name. Name derivation alone would therefore let the first
stop and delete the second's agent while never naming them. So every read and every
mutation re-reads the object and checks its `agent.enterprise-ai/user` LABEL against the
caller — the label is written from the authenticated name at create time and is what the
-914 meter already attributes by. Derivation picks the candidate; the label is the guard.
`test_portal_agents.py::test_a_hyphen_collision_cannot_reach_another_users_agent` is that
case, and it is the reason this is not three lines of string formatting.

WHAT AUTHORITY THIS HOLDS

`deploy/k8s/39-control-plane-rbac.yaml` grants create/get/list/patch/delete on five
namespaced resource kinds. That is real privilege — enough to delete any agent, any
workspace Secret, any ConfigMap in `enterprise-ai` — and Kubernetes cannot scope it per
user, because RBAC has no notion of "the objects whose label matches the caller". The
owner check in this file is therefore the ONLY thing standing between one camper and every
other camper's agent. It is tested as a security control, not as a happy path.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
from pathlib import Path

import httpx
import yaml
from fastapi import HTTPException

from . import agent_usage, db, gateway, issuance

# One source for the in-cluster credential and the API address. See the module docstring.
KUBE_API = agent_usage.KUBE_API
COMPONENT_SELECTOR = agent_usage.COMPONENT_SELECTOR
USER_LABEL = agent_usage.USER_LABEL
NAME_LABEL = agent_usage.NAME_LABEL
MODEL_SOURCE_LABEL = agent_usage.MODEL_SOURCE_LABEL
TYPE_LABEL = agent_usage.TYPE_LABEL
AGENT_TYPES = agent_usage.AGENT_TYPES
DEFAULT_AGENT_TYPE = agent_usage.DEFAULT_AGENT_TYPE

# The k8s object-name budget. `agent-<user>-<name>` must fit inside RFC 1123's 63
# characters, and the answer to overflow is REFUSAL, never truncation: a truncated name
# collides with somebody else's agent and silently shares its volume. Same ruling, same
# words, as deploy/bin/provision-agent.sh.
MAX_OBJECT_NAME = 63

# opencode's default model for a new agent. The same default provision-agent.sh carries,
# and overridable per deployment rather than per request — a model name from an untrusted
# request body ends up in a pod spec.
DEFAULT_MODEL = os.environ.get("AGENT_MODEL", "glm-5.2@deepinfra")

GATEWAY_BASE = os.environ.get("AGENT_GATEWAY_BASE", "http://gateway:4000/v1")

# The hermes image a gateway agent runs (agents-gateway-console.md). NOT the workspace
# image — a gateway agent is the Agents pillar, not opencode — so it is a fixed, overridable
# tag rather than read off a live workspace pod. Defaults to the tag the live hand-applied
# agents run, verified on the cluster by enterpriseaiframework-2ba.
HERMES_IMAGE = os.environ.get("AGENT_HERMES_IMAGE", "nousresearch/hermes-agent:v2026.8.3")

# The dashboard's basic-auth username, seeded into config.yaml (Contract D). Fixed, not
# per-agent: the control-plane console proxy (enterpriseaiframework-8e4) authenticates with
# it, and the per-agent secret is the password, never the username.
DASHBOARD_USERNAME = "console"

# The hermes dashboard's native port (agents-gateway-console.md Contract C), the port the
# per-agent Service publishes and the console proxy targets. Confirmed :9119 by -2ba.
DASHBOARD_PORT = int(os.environ.get("AGENT_DASHBOARD_PORT", "9119"))

# The object set and the resident entrypoint, delivered to this pod as a ConfigMap because
# the control-plane image is built from `control-plane/` alone and these files live under
# `deploy/`. Rendering the SAME bytes provision-agent.sh renders is the point: a second
# copy of the Deployment shape in Python would drift from the template within a release,
# and the drift would be invisible until an agent came up subtly different from the one an
# operator provisioned by hand.
ASSETS_DIR = Path(os.environ.get("AGENT_ASSETS_DIR", "/etc/agent-assets"))

_REPO = Path(__file__).resolve().parents[2]

# EVERY file the agent pod mounts at /etc/agent, in the order provision-agent.sh lists
# them — the entrypoint, every outside-world tool the entrypoint puts on PATH, and the
# instructions that tell opencode each tool exists.
#
# THIS USED TO BE `entrypoint.sh` ALONE, and that was worse than incomplete. The ConfigMap
# is deployment-wide and shared by every agent, and `_apply` is a server-side apply with
# `force=true`: a create from the portal did not merely omit `agent-slack` from the agent
# it was making, it REPLACED the ConfigMap and took the chat and mail tools away from
# every agent an operator had already provisioned. Their credentials stayed in their
# environments and the tools that read them vanished, which presents as an agent that has
# stopped answering in Slack for no reason anybody changed.
#
# `agentws.py` is a MODULE, not a command: the RFC 6455 client both chat tools import, and
# it must sit in the directory they add to sys.path. It ships here rather than in the image
# because Contract 6 freezes deploy/workspace/, including the Dockerfile.
AGENT_FILES = (
    "entrypoint.sh", "agent-email", "EMAIL.md", "agent-slack", "SLACK.md",
    "agent-discord", "DISCORD.md", "agentws.py",
)

_ASSET_FALLBACK = {
    "64-agent.template.yaml": _REPO / "deploy" / "k8s" / "64-agent.template.yaml",
    "65-agent-hermes.template.yaml": _REPO / "deploy" / "k8s" / "65-agent-hermes.template.yaml",
    **{name: _REPO / "deploy" / "agent" / name for name in AGENT_FILES},
}

# What the pod's Secret holds before a real key is written into it. -055's sentinel,
# spelled identically so a grep for a 401 finds the same item from either direction.
KEY_SENTINEL = "unset-pending-enterpriseaiframework-39d"

# Status strings this module reports. `created` is deliberately distinct from `running`:
# replicas is 1 but no pod is Running yet, and calling that "running" is how a page comes
# to show a green dot next to an agent that is in ImagePullBackOff.
RUNNING, STARTING, STOPPED, UNKNOWN = "running", "starting", "stopped", "unknown"


# ---------------------------------------------------------------- connectors
#
# WHAT A CONNECTOR IS AND WHY IT IS HERE RATHER THAN IN A SHELL SCRIPT
#
# -a4e gave an agent a mailbox and -783 gave it a Slack workspace and a Discord guild.
# Both landed as `deploy/bin/provision-agent.sh --slack-config-file …`, which is an
# operator on an operator's laptop with an operator's kubeconfig. So a user could create
# an agent from the browser (-627) and then could not give it the one thing that makes it
# useful without finding an operator. This section is that last step moved into the
# product: the SAME Secrets, the SAME key names, the SAME checksum annotations, written by
# the control-plane pod on behalf of an authenticated person.
#
# It is deliberately not a second schema. Every field below is quoted from the allowlists
# provision-agent.sh passes to `provision_connector`, and
# `control-plane/tests/test_portal_connectors.py::test_the_python_schema_matches_the_shell
# _provisioners_allowlists` parses that script and fails if the two ever diverge — because
# a key this file accepts and the shell script does not is a key the agent tools will
# never read, and a key the shell accepts and this does not is a setting a user cannot
# reach from the browser.
#
# THE ALLOWLIST IS A SECURITY CONTROL, NOT VALIDATION POLISH. The pod injects each of
# these Secrets with `envFrom`, so every key in one becomes an environment variable in a
# container that holds a spendable API key. A request body carrying `LD_PRELOAD` or
# `PATH=/tmp/evil` would be arbitrary code execution dressed up as a chat setting. The
# template's explicit `env:` wins over `envFrom` for OPENAI_API_KEY and
# OPENCODE_SERVER_PASSWORD, but that only defends the two names somebody thought of.

class Connector:
    """One connector's Secret name, its allowed keys, and what it cannot go without."""

    def __init__(self, kind: str, *, allowed: tuple[str, ...],
                 required: tuple[str, ...], sum_key: str, noun: str):
        self.kind = kind
        self.allowed = allowed
        self.required = required
        self.sum_key = sum_key
        self.noun = noun

    def secret_name(self, obj: str) -> str:
        return f"{obj}-{self.kind}"

    @property
    def annotation(self) -> str:
        # The pod template's `checksum/<connector>` annotation. Changing it is what rolls
        # the pod, and rolling the pod is the only way a re-supplied credential reaches a
        # running agent: `envFrom` is injected at pod start and never updated afterwards.
        return f"checksum/{self.kind}"


CONNECTORS: dict[str, Connector] = {
    # BOTH Slack tokens are required, for the reason provision-agent.sh states: the bot
    # token posts, the app-level token opens the Socket Mode websocket that RECEIVES. An
    # agent given only the first can talk and can never listen, which presents as "it
    # ignores me" long after the configuration that caused it.
    "slack": Connector(
        "slack",
        allowed=("AGENT_SLACK_BOT_TOKEN", "AGENT_SLACK_APP_TOKEN",
                 "AGENT_SLACK_DEFAULT_CHANNEL", "AGENT_SLACK_API_BASE",
                 "AGENT_SLACK_CA_FILE"),
        required=("AGENT_SLACK_BOT_TOKEN", "AGENT_SLACK_APP_TOKEN"),
        sum_key="AGENT_SLACK_CONFIG_SUM",
        noun="Slack setting",
    ),
    # Discord needs ONE token for both directions — the same bot token authenticates the
    # REST call that posts and the Gateway websocket that listens.
    "discord": Connector(
        "discord",
        allowed=("AGENT_DISCORD_BOT_TOKEN", "AGENT_DISCORD_DEFAULT_CHANNEL",
                 "AGENT_DISCORD_API_BASE", "AGENT_DISCORD_API_VERSION",
                 "AGENT_DISCORD_INTENTS", "AGENT_DISCORD_CA_FILE"),
        required=("AGENT_DISCORD_BOT_TOKEN",),
        sum_key="AGENT_DISCORD_CONFIG_SUM",
        noun="Discord setting",
    ),
    # A mailbox that could send and not read, or read and not send, is not a configuration
    # this surface offers — hence four required keys rather than one.
    "email": Connector(
        "email",
        allowed=("AGENT_EMAIL_ADDRESS", "AGENT_EMAIL_USERNAME", "AGENT_EMAIL_PASSWORD",
                 "AGENT_EMAIL_SMTP_HOST", "AGENT_EMAIL_SMTP_PORT",
                 "AGENT_EMAIL_SMTP_SECURITY", "AGENT_EMAIL_IMAP_HOST",
                 "AGENT_EMAIL_IMAP_PORT", "AGENT_EMAIL_IMAP_SECURITY",
                 "AGENT_EMAIL_CA_FILE"),
        required=("AGENT_EMAIL_ADDRESS", "AGENT_EMAIL_PASSWORD",
                  "AGENT_EMAIL_SMTP_HOST", "AGENT_EMAIL_IMAP_HOST"),
        sum_key="AGENT_EMAIL_CONFIG_SUM",
        noun="mail setting",
    ),
}

# What the template's checksum annotation says when an agent has no such connector. The
# literal provision-agent.sh writes, so an agent provisioned either way reads the same.
NO_CONNECTOR = "none"

# A credential is not this long. The cap exists because the whole body ends up in a Secret
# and then in a pod's environment, and an unbounded string from a request body is a way to
# make a pod fail to start (the kernel's argument/environment limit) from the browser.
MAX_CONNECTOR_VALUE = 4096


# ---------------------------------------------------------------- assets


def asset(name: str) -> str:
    """One of the two files this module renders, from the ConfigMap or from the checkout.

    In the pod it is the mounted ConfigMap. In a checkout — the hermetic tests, the local
    harness — it is the repository file itself, which is the same source the ConfigMap is
    built from. There is no third copy and no embedded default: an agent rendered from a
    stale in-image duplicate is worse than a create that refuses.
    """
    mounted = ASSETS_DIR / name
    if mounted.is_file():
        return mounted.read_text()
    fallback = _ASSET_FALLBACK.get(name)
    if fallback and fallback.is_file():
        return fallback.read_text()
    raise HTTPException(
        503,
        f"the agent asset {name!r} is not available to this control plane. It is mounted "
        f"from the `agent-assets` ConfigMap (deploy/bin/deploy.sh builds it from "
        f"deploy/k8s/64-agent.template.yaml and deploy/agent/entrypoint.sh); without it "
        "an agent cannot be rendered.",
    )


# ---------------------------------------------------------------- names


def object_name(user: str, name: str) -> str:
    """`agent-<user>-<name>`, validated, or an HTTP error explaining exactly what is wrong.

    Both halves are held to the SAME slug the workspace enforces on project names and
    provision-agent.sh enforces on agent names. It is load-bearing twice over: it keeps
    Contract 1's alias `<user>::agents/<name>` to one `::` and no `/`, and it keeps both
    halves inside the character set Kubernetes accepts in an object name.
    """
    if not gateway.AGENT_SLUG.match(name or ""):
        raise HTTPException(
            400,
            f"an agent name must match {gateway.AGENT_SLUG.pattern} — lower-case letters, "
            "digits and hyphens, starting with a letter or digit. Names are constrained "
            "rather than rewritten: a rejected name is easy to explain, a silently "
            "altered one is not.",
        )
    if not gateway.AGENT_SLUG.match(user or ""):
        # A username that cannot be part of an object name is a deployment-level problem
        # (an identity provider handing out names with dots or capitals), not something to
        # paper over by mangling it into something that fits.
        raise HTTPException(
            400,
            f"the signed-in name {user!r} cannot form a Kubernetes object name "
            f"(must match {gateway.AGENT_SLUG.pattern}).",
        )
    obj = f"agent-{user}-{name}"
    if len(obj) > MAX_OBJECT_NAME:
        raise HTTPException(
            400,
            f"the object name {obj!r} is {len(obj)} characters, over Kubernetes' limit of "
            f"{MAX_OBJECT_NAME}. Shorten the agent name. It is not truncated on purpose: "
            "a truncated name can collide with another user's agent and silently share "
            "its volume.",
        )
    return obj


# ---------------------------------------------------------------- the API client


def _headers() -> dict:
    # Read from disk per call, never cached: projected tokens are rotated by the kubelet
    # and a process holding the boot-time value starts 401ing an hour later with nothing
    # on screen to say why. Same reasoning as app/agent_usage.py.
    return {"Authorization": f"Bearer {agent_usage._token()}"}


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(verify=agent_usage._verify(), timeout=30.0)


def namespace() -> str:
    return agent_usage.namespace()


# The five kinds this module touches, and the API path each lives at. An explicit map
# rather than a pluraliser: a wrong guess here is a 404 that reads like "the object is
# gone", which is the one diagnosis that must never be produced by a typo.
_PATHS = {
    ("v1", "Pod"): "/api/v1/namespaces/{ns}/pods",
    ("v1", "PersistentVolumeClaim"): "/api/v1/namespaces/{ns}/persistentvolumeclaims",
    ("v1", "Service"): "/api/v1/namespaces/{ns}/services",
    ("v1", "Secret"): "/api/v1/namespaces/{ns}/secrets",
    ("v1", "ConfigMap"): "/api/v1/namespaces/{ns}/configmaps",
    ("apps/v1", "Deployment"): "/apis/apps/v1/namespaces/{ns}/deployments",
}


def _collection(api_version: str, kind: str) -> str:
    try:
        return _PATHS[(api_version, kind)].format(ns=namespace())
    except KeyError:
        raise HTTPException(
            500,
            f"the agent template asked for {api_version}/{kind}, which this control plane "
            "holds no authority over. Widening deploy/k8s/39-control-plane-rbac.yaml is a "
            "security decision and is not made here.",
        )


async def _apply(client: httpx.AsyncClient, obj: dict) -> dict:
    """Server-side apply one object. The same semantics `kubectl apply` has.

    PATCH with `application/apply-patch+yaml` rather than create-then-fall-back-to-update:
    the fall-back version has a race (two creates, one loses with 409) and it silently
    strips fields another manager owns. `force=true` takes ownership of the fields this
    manager sets, which is what lets the portal re-apply an agent an operator first
    provisioned with `kubectl apply` from provision-agent.sh.
    """
    name = obj["metadata"]["name"]
    url = f"{KUBE_API}{_collection(obj['apiVersion'], obj['kind'])}/{name}"
    resp = await client.patch(
        url,
        params={"fieldManager": "enterprise-ai-control-plane", "force": "true"},
        headers={**_headers(), "Content-Type": "application/apply-patch+yaml"},
        json=obj,
    )
    _raise_for_kube(resp, f"apply {obj['kind']}/{name}")
    return resp.json()


async def _get(client: httpx.AsyncClient, api_version: str, kind: str,
               name: str) -> dict | None:
    resp = await client.get(
        f"{KUBE_API}{_collection(api_version, kind)}/{name}", headers=_headers()
    )
    if resp.status_code == 404:
        return None
    _raise_for_kube(resp, f"read {kind}/{name}")
    return resp.json()


async def _list(client: httpx.AsyncClient, api_version: str, kind: str,
                selector: str) -> list[dict]:
    resp = await client.get(
        f"{KUBE_API}{_collection(api_version, kind)}",
        params={"labelSelector": selector},
        headers=_headers(),
    )
    _raise_for_kube(resp, f"list {kind}")
    return resp.json().get("items", [])


async def _delete(client: httpx.AsyncClient, api_version: str, kind: str,
                  name: str) -> bool:
    """Delete one object. Returns whether it was there. 404 is success, not an error."""
    resp = await client.delete(
        f"{KUBE_API}{_collection(api_version, kind)}/{name}", headers=_headers()
    )
    if resp.status_code == 404:
        return False
    _raise_for_kube(resp, f"delete {kind}/{name}")
    return True


async def _patch(client: httpx.AsyncClient, api_version: str, kind: str, name: str,
                 body: dict) -> dict:
    resp = await client.patch(
        f"{KUBE_API}{_collection(api_version, kind)}/{name}",
        headers={**_headers(), "Content-Type": "application/strategic-merge-patch+json"},
        json=body,
    )
    _raise_for_kube(resp, f"patch {kind}/{name}")
    return resp.json()


def _raise_for_kube(resp: httpx.Response, what: str) -> None:
    """Turn an API-server refusal into an HTTP error that names the cause.

    A 403 here means the RBAC grant is missing a verb, and it must not reach a user as a
    generic 500: that is a deployment fault with a one-line fix, and the message says so.
    """
    if resp.status_code < 400:
        return
    detail = ""
    try:
        detail = resp.json().get("message", "")
    except Exception:  # noqa: BLE001 - the status is the diagnosis when the body is not JSON
        detail = resp.text[:400]
    if resp.status_code == 403:
        raise HTTPException(
            500,
            f"the control plane is not permitted to {what}: {detail}. Apply "
            "deploy/k8s/39-control-plane-rbac.yaml and restart the control-plane pod.",
        )
    raise HTTPException(502, f"could not {what}: {resp.status_code} {detail}")


# ---------------------------------------------------------------- ownership


def _owner_of(obj: dict) -> str:
    return ((obj.get("metadata") or {}).get("labels") or {}).get(USER_LABEL, "")


def _agent_of(obj: dict) -> str:
    return ((obj.get("metadata") or {}).get("labels") or {}).get(NAME_LABEL, "")


async def _owned_deployment(client: httpx.AsyncClient, user: str, name: str) -> dict:
    """The caller's own agent Deployment, or an error that reveals nothing about others.

    THE GUARD. `object_name` derives a candidate from the authenticated name, and then
    this re-reads the object and insists its labels say the same thing. See the module
    docstring for the hyphen collision that makes the second half necessary.

    404 for "not yours" as well as for "not there". A distinct 403 would confirm to a
    prober that an agent by that name exists and belongs to somebody — which is the one
    fact a listing endpoint exists to keep private.
    """
    obj = await _get(client, "apps/v1", "Deployment", object_name(user, name))
    if obj is None or _owner_of(obj) != user or _agent_of(obj) != name:
        raise HTTPException(404, f"you have no agent called {name!r}")
    return obj


# ---------------------------------------------------------------- status


def _status_of(deployment: dict, pod_phase: str | None) -> str:
    """The four Contract 2 states, read off the cluster rather than off a stored flag.

    `stopped` is `replicas: 0` and nothing else — that is the whole reason Contract 2 says
    scale-to-zero: there is no pod, so the -914 meter has nothing to sample and the usage
    numbers freeze as a consequence of the mechanism rather than as a billing rule.
    """
    replicas = (deployment.get("spec") or {}).get("replicas")
    if replicas == 0:
        return STOPPED
    if pod_phase == "Running":
        return RUNNING
    if replicas and replicas > 0:
        return STARTING
    return UNKNOWN


async def _pod_phases(client: httpx.AsyncClient, user: str) -> dict[str, str]:
    """`{agent name: pod phase}` for one owner, from the pod read -914 already grants."""
    pods = await _list(
        client, "v1", "Pod", f"{COMPONENT_SELECTOR},{USER_LABEL}={user}"
    )
    out: dict[str, str] = {}
    for pod in pods:
        agent = _agent_of(pod)
        phase = (pod.get("status") or {}).get("phase") or ""
        if not agent:
            continue
        # Running wins over a terminating predecessor: during a Recreate rollout both are
        # briefly listed, and reporting the dying one would flip a healthy agent to
        # "starting" for no reason a user could act on.
        if out.get(agent) != "Running":
            out[agent] = phase
    return out


async def list_agents(user: str) -> list[dict]:
    """Every agent this user owns, with its live status. Never anybody else's.

    The label selector carries the owner, so the API server itself filters — a mistake in
    this function cannot return another user's row, because another user's row was never
    fetched. The label check in `_owned_deployment` covers the mutating paths, where a
    name is supplied.
    """
    async with _client() as client:
        deployments = await _list(
            client, "apps/v1", "Deployment",
            f"{COMPONENT_SELECTOR},{USER_LABEL}={user}",
        )
        phases = await _pod_phases(client, user)

    out = []
    for dep in deployments:
        name = _agent_of(dep)
        # Both halves of the attribution key, and the name still has to be one this
        # deployment could have issued. An object hand-labelled with a name outside the
        # slug is skipped rather than rendered: it cannot have a valid Contract 1 alias, so
        # there is nothing to line its spend up against, and letting it through would take
        # the whole list down with a 500 for every other agent the user owns.
        if not name or _owner_of(dep) != user or not gateway.AGENT_SLUG.match(name):
            continue
        labels = (dep.get("metadata") or {}).get("labels") or {}
        out.append({
            "name": name,
            # Contract 1's per-instance surface: the join key to this agent's inference
            # spend and to its -914 usage row, so a caller lines the two dimensions up
            # without parsing anything.
            "surface": gateway.agent_surface(name),
            "status": _status_of(dep, phases.get(name)),
            "pod_phase": phases.get(name, ""),
            "model_source": labels.get(MODEL_SOURCE_LABEL, ""),
            # The Agents-pillar type (Contract A): reported straight off the label, empty
            # for an agent minted before the dimension existed — read as raw as
            # model_source above rather than defaulted, so the listing never claims a type
            # the pod was not labelled with.
            "type": labels.get(TYPE_LABEL, ""),
            "replicas": (dep.get("spec") or {}).get("replicas", 0),
            "created_at": (dep.get("metadata") or {}).get("creationTimestamp", ""),
            # Where -0e7 will attach a console. The portal renders the entry now so the
            # surface is complete from a user's side the moment the proxy lands; the path
            # never carries the owner, which is Contract 1's rule for exactly this URL.
            "console_url": console_url(name),
            # Which connectors this agent has — booleans, never key names and never
            # values. It is read from the pod template the agent is running with, so it
            # costs no extra API call and cannot claim a credential the pod never saw.
            "connectors": _connector_state(dep),
        })
    return sorted(out, key=lambda a: a["name"])


def console_url(name: str) -> str:
    """`/agents/<name>/` — the owner is resolved from the session, never from the path."""
    return f"/agents/{name}/"


# The port `opencode serve` binds in the agent pod, and the port the per-agent Service
# publishes. One name, spelled the same as the variable deploy/agent/entrypoint.sh reads
# (`AGENT_SERVE_PORT`), so moving it moves both ends at once.
SERVE_PORT = int(os.environ.get("AGENT_SERVE_PORT", "4096"))

# The username half of the daemon's HTTP Basic credential. `opencode serve` ignores it and
# checks only the password (deploy/agent/entrypoint.sh records the measurement), but a
# Basic header needs both halves and this is the one every other caller uses —
# tests-live/test_agent_resident.py curls `-u opencode:$password`.
CONSOLE_BASIC_USER = "opencode"


async def console_target(user: str, name: str) -> dict:
    """Where the caller's OWN resident daemon is, and the credential to speak to it.

    This is the whole owner-scoping of the console proxy (enterpriseaiframework-0e7), and
    it is deliberately the SAME guard the stop/start/delete endpoints use rather than a
    second implementation of it: `_owned_deployment` derives `agent-<user>-<name>` from the
    authenticated name and then re-reads the object and insists its labels say the same
    thing. See the module docstring for the hyphen collision that makes the second half
    necessary — without it `alice` asking for the console of `bot-two` would attach to
    `alice-bot`'s agent `two`, which is a live session and a spendable key.

    It returns a host and a password, not an open connection, so that the proxy in
    `agent_console.py` holds no authorisation logic at all: there is no code path there
    that can reach a daemon this function did not name.

    404 for "not yours" as well as for "not there", for the reason `_owned_deployment`
    gives — a distinct 403 confirms to a prober that somebody owns an agent by that name.
    """
    obj = object_name(user, name)
    async with _client() as client:
        deployment = await _owned_deployment(client, user, name)
        secret = await _get(client, "v1", "Secret", f"{obj}-key")

    data = (secret or {}).get("data") or {}
    labels = (deployment.get("metadata") or {}).get("labels") or {}
    # Read the type as raw as list_agents does — NOT defaulted to hermes. This resolves an
    # EXISTING agent's console: only an explicit `hermes` label selects the gateway console;
    # a missing label is a pre-dimension opencode agent and takes the Basic-auth path below.
    agent_type = labels.get(TYPE_LABEL, "")

    def _decode(key: str) -> str:
        raw = data.get(key)
        return base64.b64decode(raw).decode() if raw else ""

    if agent_type == "hermes":
        # The Agents-pillar console: the agent's OWN hermes dashboard (Contract C). The
        # proxy authenticates to it with the console credential -f55 seeded into the key
        # Secret (form-login -> session cookie; the dashboard's basic-auth gate is not HTTP
        # Basic), so the user reaches it through the portal's Keycloak session and never the
        # dashboard's own login. `host` is the per-agent Service; `port` its native 9119.
        password = _decode("DASHBOARD_PASSWORD")
        if not password:
            raise HTTPException(
                503,
                f"the hermes agent {name!r} has no console credential in Secret {obj}-key. "
                "It is written at create time; re-provision the agent rather than attaching.",
            )
        return {
            "type": "hermes",
            "host": obj,
            "port": DASHBOARD_PORT,
            "username": (_decode("DASHBOARD_USERNAME") or DASHBOARD_USERNAME),
            "password": password,
        }

    # The opencode/interim path: HTTP Basic on the resident daemon. Unchanged.
    encoded = data.get("OPENCODE_SERVER_PASSWORD")
    if not encoded:
        # The daemon refuses to start without this (deploy/agent/entrypoint.sh), so a
        # missing one means the Secret was replaced or hand-edited. Attaching without it
        # would 401 at the daemon and read as "the agent is broken".
        raise HTTPException(
            503,
            f"the agent {name!r} has no console credential in its Secret {obj}-key. It is "
            "written at create time and the daemon refuses to start without it; "
            "re-provision the agent rather than attaching to it.",
        )
    return {
        "type": agent_type,
        "host": obj,
        "port": SERVE_PORT,
        "username": CONSOLE_BASIC_USER,
        "password": base64.b64decode(encoded).decode(),
    }


# ---------------------------------------------------------------- create


def render(user: str, name: str, *, image: str, model: str, api_base: str,
           model_source: str, key_secret: str, cfgsum: str, keysum: str,
           connector_sums: dict[str, str] | None = None) -> list[dict]:
    """The template, substituted exactly as provision-agent.sh substitutes it.

    Literal replacement of the same twelve placeholders, then a YAML parse — not a
    hand-built object graph. The template is the design (its comments are the reasoning
    for every field in it) and this is a second renderer of it, not a second copy.

    THE THREE CONNECTOR CHECKSUMS ARE PART OF THAT SET and were missed when -627's
    renderer was written, because -a4e and -783 added them to the template afterwards.
    An agent created from the portal therefore carried the literal string
    `checksum/slack: "__SLACKSUM__"` — a valid annotation value, so nothing failed, and
    the first credential written to it would then produce a checksum that DID change and
    roll the pod anyway. It is fixed rather than worked around because the next
    placeholder somebody adds to the template must not be silently ignored by this path:
    `test_creating_an_agent_applies_the_real_template_with_every_placeholder_filled` is
    the check, and it was already red against the merged Agents surface.
    """
    sums = connector_sums or {}
    text = asset("64-agent.template.yaml")
    for placeholder, value in (
        ("__USER__", user), ("__NAME__", name), ("__IMAGE__", image),
        ("__MODEL__", model), ("__CFGSUM__", cfgsum), ("__KEYSUM__", keysum),
        ("__MODEL_SOURCE__", model_source), ("__API_BASE__", api_base),
        ("__KEY_SECRET__", key_secret),
        ("__EMAILSUM__", sums.get("email") or NO_CONNECTOR),
        ("__SLACKSUM__", sums.get("slack") or NO_CONNECTOR),
        ("__DISCORDSUM__", sums.get("discord") or NO_CONNECTOR),
    ):
        text = text.replace(placeholder, value)
    docs = [d for d in yaml.safe_load_all(text) if d]
    if not docs:
        raise HTTPException(500, "the agent template rendered to nothing")
    return docs


def _stamp_agent_type(docs: list[dict], agent_type: str) -> None:
    """Carry `agent.enterprise-ai/type` on the Deployment and its pod template.

    Both placements mirror `agent.enterprise-ai/model-source` exactly
    (deploy/k8s/64-agent.template.yaml lines 92 and 129): the object label is what
    `list_agents` reads, and the pod-template label is what a per-type console/model
    selector reads off the running pod.

    Stamped here in Python, NOT as a `__TYPE__` placeholder, on purpose. The interim
    renderer is the opencode-based `64-agent.template.yaml` — the Code pillar's harness —
    and the design record (agents-gateway-console.md, Contract A) is explicit that this
    template is NOT extended for gateway agents. The per-type hermes/openclaw templates
    (enterpriseaiframework-f55 / -ff7) carry the label natively and select the image, run
    command and native-console port by it; until they land, this stamps the dimension so
    the create/list/wizard plumbing is real and testable. Additive — it only adds a label,
    never removes one the template already set.
    """
    for doc in docs:
        if doc.get("kind") != "Deployment":
            continue
        meta = doc.setdefault("metadata", {})
        meta.setdefault("labels", {})[TYPE_LABEL] = agent_type
        pod_meta = (
            doc.setdefault("spec", {})
            .setdefault("template", {})
            .setdefault("metadata", {})
        )
        pod_meta.setdefault("labels", {})[TYPE_LABEL] = agent_type


# ---------------------------------------------------------------- hermes (Agents pillar)

# scrypt parameters, IDENTICAL to hermes's plugins/dashboard_auth/basic.hash_password
# (verified against the real image by enterpriseaiframework-2ba): n=2**14, r=8, p=1,
# dklen=32, 16-byte salt, hash string `scrypt$16384$8$1$<salt_b64>$<dk_b64>`. The control
# plane computes the hash with stdlib scrypt so the seed carries only the HASH; the
# plaintext lives in the agent's key Secret for the console proxy (enterpriseaiframework-8e4).
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P, _SCRYPT_DKLEN, _SCRYPT_SALT_BYTES = 2**14, 8, 1, 32, 16


def dashboard_password_hash(password: str) -> str:
    """A hermes-compatible scrypt hash string for the dashboard basic_auth password.

    Reproduces hermes's own ``hash_password`` byte-for-byte so the value seeded into
    ``dashboard.basic_auth.password_hash`` verifies against the plaintext the console proxy
    presents. Format and parameters are pinned to the real binary; a drift here would fail
    closed (the dashboard would reject a correct password) rather than open.
    """
    salt = secrets.token_bytes(_SCRYPT_SALT_BYTES)
    dk = hashlib.scrypt(
        password.encode(), salt=salt,
        n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_SCRYPT_DKLEN, maxmem=0,
    )
    return (
        f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}$"
        f"{base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"
    )


def hermes_seed_config(model: str, password_hash: str) -> str:
    """The FIRST-BOOT-ONLY config.yaml for a hermes agent (Contract B).

    Mirrors the shape the live agents run (verified by -2ba): the integrated gateway
    provider, the default model, and the dashboard's basic-auth so a non-loopback bind
    satisfies hermes's fail-closed auth gate. After first boot this is never re-applied —
    the agent's own config on the PVC is authoritative and settings change through the
    dashboard API (Contract D), so this seed is a starting point, not a source of truth.
    """
    return yaml.safe_dump(
        {
            "providers": {
                "gateway": {
                    "base_url": GATEWAY_BASE,
                    "key_env": "OPENAI_API_KEY",
                    "discover_models": True,
                },
            },
            "model": {"provider": "gateway", "default": model},
            "dashboard": {
                "basic_auth": {
                    "username": DASHBOARD_USERNAME,
                    "password_hash": password_hash,
                },
            },
            "terminal": {"backend": "local"},
        },
        default_flow_style=False,
        sort_keys=False,
    )


def render_hermes(user: str, name: str, *, image: str, model_source: str,
                  key_secret: str, cfgsum: str, keysum: str,
                  connector_sums: dict[str, str] | None = None) -> list[dict]:
    """Render 65-agent-hermes.template.yaml, exactly as render() renders the opencode one.

    Literal replacement of the placeholders, then a YAML parse — the template is the design
    (Contract B's two-container/first-boot-seed shape) and this is its renderer. The
    ``agent.enterprise-ai/type: hermes`` label is written into the template itself, so no
    post-render stamp is needed for this type.
    """
    sums = connector_sums or {}
    text = asset("65-agent-hermes.template.yaml")
    for placeholder, value in (
        ("__USER__", user), ("__NAME__", name), ("__IMAGE__", image),
        ("__MODEL_SOURCE__", model_source), ("__KEY_SECRET__", key_secret),
        ("__CFGSUM__", cfgsum), ("__KEYSUM__", keysum),
        ("__EMAILSUM__", sums.get("email") or NO_CONNECTOR),
        ("__SLACKSUM__", sums.get("slack") or NO_CONNECTOR),
        ("__DISCORDSUM__", sums.get("discord") or NO_CONNECTOR),
    ):
        text = text.replace(placeholder, value)
    docs = [d for d in yaml.safe_load_all(text) if d]
    if not docs:
        raise HTTPException(500, "the hermes agent template rendered to nothing")
    return docs


async def _workspace_image(client: httpx.AsyncClient) -> str:
    """The image a new agent runs: whatever the Code surface is ACTUALLY running now.

    Read off a live workspace pod rather than computed from a tag, for the reason
    tests-live/test_agent_resident.py records: the tag deployed on a cluster is routinely
    not this checkout's HEAD, and an agent that named a tag the registry has never seen
    would fail as an ImagePullBackOff that reads like a cluster problem. Contract 6 says
    the agent reuses the workspace ARTEFACT; this reads the artefact rather than guessing
    its name.
    """
    override = os.environ.get("AGENT_IMAGE")
    if override:
        return override
    pods = await _list(client, "v1", "Pod", "app.kubernetes.io/component=workspace")
    for pod in pods:
        if (pod.get("status") or {}).get("phase") != "Running":
            continue
        for container in (pod.get("spec") or {}).get("containers", []):
            if container.get("name") == "ttyd" and container.get("image"):
                return container["image"]
    raise HTTPException(
        503,
        "no running workspace pod to read the agent image from, and AGENT_IMAGE is not "
        "set. An agent runs the same image the Code surface runs (Contract 6); there is "
        "deliberately no fallback tag, because a guessed tag fails as an "
        "ImagePullBackOff that looks like a cluster fault.",
    )


def _secret_object(name: str, data: dict[str, str],
                   labels: dict[str, str] | None = None) -> dict:
    return {
        "apiVersion": "v1", "kind": "Secret",
        "metadata": {"name": name, "namespace": namespace(), "labels": {
            "app.kubernetes.io/part-of": "enterprise-ai-framework",
            "app.kubernetes.io/component": "agent",
            **(labels or {}),
        }},
        "type": "Opaque",
        "data": {k: base64.b64encode(v.encode()).decode() for k, v in data.items()},
    }


async def _existing_connector_sums(client: httpx.AsyncClient, obj: str) -> dict[str, str]:
    """The checksum already stored beside each connector credential, or nothing.

    `provision_connector`'s `existing_in`, in Python. It exists so a render that is not
    supplying a credential produces the SAME annotation the last one did and therefore
    does NOT restart a healthy agent — the sum is stored in the Secret precisely because
    neither this code nor the shell script reads the credential back to derive it.
    """
    out: dict[str, str] = {}
    for kind, spec in CONNECTORS.items():
        secret = await _get(client, "v1", "Secret", spec.secret_name(obj))
        encoded = ((secret or {}).get("data") or {}).get(spec.sum_key)
        if encoded:
            out[kind] = base64.b64decode(encoded).decode()
    return out


def _connector_state(deployment: dict) -> dict[str, bool]:
    """Which connectors this agent actually has, read off the POD TEMPLATE.

    The source is the `checksum/<connector>` annotation rather than the existence of the
    Secret, and that is the more honest of the two: the annotation is what the pod is
    running with. A Secret written without rolling the pod would show as configured while
    the agent had never read it, which is exactly the failure this surface exists to make
    impossible.

    `none` is provision-agent.sh's literal for "this agent has no such connector", and an
    unsubstituted `__SLACKSUM__` (every agent created by -627 before this change) means
    the same thing — neither is a credential.
    """
    annotations = (((deployment.get("spec") or {}).get("template") or {})
                   .get("metadata") or {}).get("annotations") or {}
    out = {}
    for kind, spec in CONNECTORS.items():
        value = (annotations.get(spec.annotation) or "").strip()
        out[kind] = bool(value) and value != NO_CONNECTOR and not value.startswith("__")
    return out


def _clean_connector_values(spec: Connector, values: dict) -> dict[str, str]:
    """The user's credential, checked before it can become a pod's environment.

    Four refusals, each of which is a real way for a browser form to produce something
    that is not a credential:

    1. A KEY OUTSIDE THE ALLOWLIST. The security control — see the section header. Not
       silently dropped: a dropped key is a setting the user believes they supplied.
    2. A NON-STRING VALUE. JSON can carry a list or an object; a Secret cannot.
    3. A CONTROL CHARACTER, including CR and LF. This is the injection refusal. The shell
       path refuses a whole CRLF file with a diagnosis because `token\\r` fails as a 401
       nobody connects to the file they saved on Windows; here a newline pasted into an
       input would do the same, and an interior newline in a value that is later read as
       KEY=value lines is a way to smuggle a second key in.
    4. AN OVERLONG VALUE.

    It DOES strip surrounding whitespace, which is the one place this path deliberately
    differs from `--slack-config-file` (where values are taken literally, quotes and all).
    The reason is the input channel: a token pasted into an HTML field routinely arrives
    with a trailing space or newline from the copy, no credential in any of these three
    schemas has meaningful leading or trailing whitespace, and refusing the paste that
    every user makes would be a worse answer than trimming it. Anything left inside the
    value after trimming is refused, not trimmed.
    """
    if not isinstance(values, dict):
        raise HTTPException(400, "the credential must be an object of KEY: value pairs")

    cleaned: dict[str, str] = {}
    for key, raw in values.items():
        if key not in spec.allowed:
            raise HTTPException(
                400,
                f"{key!r} is not a {spec.noun}. This becomes the agent pod's environment "
                f"via envFrom, so an unexpected key here would be an environment variable "
                f"in a container that holds a spendable API key. Allowed: "
                f"{', '.join(spec.allowed)}.",
            )
        if raw is None:
            continue
        if not isinstance(raw, str):
            raise HTTPException(400, f"{key} must be a string.")
        value = raw.strip()
        if not value:
            continue
        if any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
            raise HTTPException(
                400,
                f"{key} contains a line break or a control character. Paste the value "
                "alone, without the surrounding line — a credential with an invisible "
                "carriage return in it fails as an authentication error that points at "
                "nothing.",
            )
        if len(value) > MAX_CONNECTOR_VALUE:
            raise HTTPException(
                400,
                f"{key} is {len(value)} characters, over the {MAX_CONNECTOR_VALUE} this "
                "surface accepts.",
            )
        cleaned[key] = value

    missing = [k for k in spec.required if k not in cleaned]
    if missing:
        raise HTTPException(
            400,
            f"missing {', '.join(missing)}. Every one of these is required, because a "
            f"half-configured connector is not a configuration this surface offers: "
            f"{', '.join(spec.required)}.",
        )
    return cleaned


def connector_sum(values: dict[str, str]) -> str:
    """The hash that goes beside the credential and into the annotation. Never the value.

    Over a canonical `KEY=value` rendering rather than the raw request body, so that the
    same credential supplied twice — in a different field order, or with a whitespace the
    trim removed — produces the SAME sum and therefore does NOT roll a healthy agent.
    Restarting an agent ends the resident session that is the entire product.
    """
    canonical = "\n".join(f"{k}={values[k]}" for k in sorted(values))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


async def configure_connector(user: str, name: str, kind: str, values: dict) -> dict:
    """Give the CALLER'S OWN agent a Slack workspace, a Discord guild or a mailbox.

    This is the last operator-only step in the Agents surface moved into the product. It
    writes the same Secret `deploy/bin/provision-agent.sh` writes, under the same name,
    with the same keys and the same `AGENT_<KIND>_CONFIG_SUM` beside them, and then bumps
    the same pod-template annotation — so `deploy/agent/agent-slack`, `agent-discord` and
    `agent-email` pick it up unchanged, and an agent configured from the browser is
    indistinguishable from one an operator provisioned.

    THE OWNER IS `require_user()` AND THE GUARD IS THE SAME ONE. `_owned_deployment`
    derives `agent-<user>-<name>` from the authenticated name and then re-reads the object
    and insists its labels agree, because two different (user, name) pairs can derive the
    same object name when either half contains a hyphen. Without the second half, `alice`
    could write a bot token into `alice-bot`'s agent — and a chat connector is worse than
    the stop button: it would put a live agent that holds somebody else's spendable model
    key into an attacker's own Slack workspace, taking instructions from them.

    404 for "not yours" as for "not there", for the reason `_owned_deployment` gives.

    NOTHING IS RETURNED BUT KEY NAMES. Set-once, never read back (Contract 4). There is
    deliberately no GET that answers with a credential, so there is no endpoint an XSS or
    a confused-deputy could read one out of, and the agent's own `agent-slack config`
    reports `bot_token_set: true` rather than the token for the same reason.
    """
    spec = CONNECTORS.get(kind) if isinstance(kind, str) else None
    if spec is None:
        raise HTTPException(
            400,
            f"unknown connector {kind!r}. This agent surface carries "
            f"{', '.join(sorted(CONNECTORS))}.",
        )
    cleaned = _clean_connector_values(spec, values)
    obj = object_name(user, name)
    checksum = connector_sum(cleaned)

    async with _client() as client:
        deployment = await _owned_deployment(client, user, name)
        labels = (deployment.get("metadata") or {}).get("labels") or {}
        await _apply(client, _secret_object(
            spec.secret_name(obj),
            # The sum is stored BESIDE the credential, exactly as the shell script stores
            # it, so a later render can reproduce this annotation without reading the
            # credential back. It is not accepted from the request (it is not in any
            # allowlist), so it cannot be spoofed into suppressing a roll.
            {**cleaned, spec.sum_key: checksum},
            # The owner labels the -914 meter and every listing already attribute by. A
            # credential Secret with no owner on it is one nothing can clean up or account
            # for; the shell path predates the label and leaves them unlabelled.
            labels={USER_LABEL: user, NAME_LABEL: name},
        ))
        # The roll. `envFrom` is injected at pod start and never updated afterwards, so
        # without this the running agent would keep presenting whatever it started with
        # and the user would watch a correctly-stored token do nothing.
        await _patch(client, "apps/v1", "Deployment", obj, {
            "spec": {"template": {"metadata": {
                "annotations": {spec.annotation: checksum},
            }}},
        })

    await db.audit(user, "agent.connector.configure", f"{user}/{name}",
                   # The KEYS, never the values. An audit trail that recorded a bot token
                   # would be a credential store with a retention policy.
                   connector=kind, keys=sorted(cleaned), checksum=checksum)
    return {
        "name": name,
        "kind": kind,
        "configured": True,
        "secret": spec.secret_name(obj),
        "keys": sorted(cleaned),
        "rolled": (deployment.get("spec") or {}).get("replicas", 0) > 0,
        "model_source": labels.get(MODEL_SOURCE_LABEL, ""),
    }


async def _provision_hermes(client: httpx.AsyncClient, user: str, name: str, obj: str,
                            model: str, api_key: str, keysum: str) -> None:
    """Apply the object set for a hermes gateway agent (agents-gateway-console.md B/D).

    Differs from the opencode path by design: no deployment-wide entrypoint ConfigMap and
    no OPENCODE_SERVER_PASSWORD — a gateway agent runs its own image and authenticates its
    console through the seeded ``dashboard.basic_auth``. The key Secret carries the
    integrated key plus the console credential (plaintext, for the -8e4 proxy); the seed
    ConfigMap carries only the password HASH.
    """
    console_password = secrets.token_urlsafe(24)
    seed = hermes_seed_config(model, dashboard_password_hash(console_password))
    cfgsum = hashlib.sha256(seed.encode()).hexdigest()[:16]

    await _apply(client, _secret_object(f"{obj}-key", {
        "OPENAI_API_KEY": api_key,
        # The console credential the -8e4 proxy presents to the dashboard. The dashboard
        # verifies the plaintext against the seeded scrypt hash; the plaintext never enters
        # the ConfigMap, only this Secret.
        "DASHBOARD_USERNAME": DASHBOARD_USERNAME,
        "DASHBOARD_PASSWORD": console_password,
    }))
    # The FIRST-BOOT seed. The initContainer copies it onto the PVC iff there is none; after
    # that the agent's own config wins (Contract B — this is the clobber the record fixes).
    await _apply(client, {
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {"name": f"{obj}-config", "namespace": namespace(),
                     "labels": {USER_LABEL: user, NAME_LABEL: name}},
        "data": {"config.yaml": seed},
    })

    sums = await _existing_connector_sums(client, obj)
    docs = render_hermes(
        user, name, image=HERMES_IMAGE, model_source="integrated",
        key_secret=f"{obj}-key", cfgsum=cfgsum, keysum=keysum, connector_sums=sums,
    )
    for doc in docs:
        await _apply(client, doc)


async def _provision_opencode_interim(client: httpx.AsyncClient, user: str, name: str,
                                      obj: str, model: str, agent_type: str,
                                      api_key: str, keysum: str) -> None:
    """The pre-gateway opencode render, kept as the interim path for a non-default type
    until its real provisioner lands (openclaw = enterpriseaiframework-ff7). Identical to
    the original create() body; the type label is stamped post-render since the opencode
    template does not carry it."""
    image = await _workspace_image(client)

    # The resident entrypoint AND every tool it puts on PATH, deployment-wide (one control
    # plane). The checksum is over ALL of them in AGENT_FILES order — byte for byte the
    # value provision-agent.sh produces, so provisioning by either route restarts nothing
    # the other created.
    files = {fname: asset(fname) for fname in AGENT_FILES}
    cfgsum = hashlib.sha256(
        "".join(files[fname] for fname in AGENT_FILES).encode()
    ).hexdigest()[:16]
    await _apply(client, {
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {"name": "agent-entrypoint", "namespace": namespace()},
        "data": files,
    })

    # HTTP Basic on the opencode server: entrypoint.sh refuses to start without it.
    password = secrets.token_urlsafe(24)
    await _apply(client, _secret_object(f"{obj}-key", {
        "OPENCODE_SERVER_PASSWORD": password,
        "OPENAI_API_KEY": api_key,
    }))

    sums = await _existing_connector_sums(client, obj)
    docs = render(
        user, name, image=image, model=model, api_base=GATEWAY_BASE,
        connector_sums=sums,
        model_source="integrated", key_secret=f"{obj}-key",
        cfgsum=cfgsum, keysum=keysum,
    )
    _stamp_agent_type(docs, agent_type)
    for doc in docs:
        await _apply(client, doc)


async def create(
    user: str, name: str, *, model: str | None = None, agent_type: str | None = None
) -> dict:
    """Contract 2's `created` transition, for the authenticated caller and nobody else.

    The order is the one provision-agent.sh uses and it is not arbitrary:

      1. refuse a name that is already somebody else's object (the hyphen collision);
      2. ensure the deployment-wide entrypoint ConfigMap exists, because the pod mounts it
         and a missing one is a pod stuck in ContainerCreating rather than an error;
      3. mint the virtual key BEFORE the pod exists, so the agent never starts holding
         -055's sentinel and 401ing on its first request with nothing on screen to say why;
      4. apply the object set.

    Minting goes through `issuance.issue`, which is the body of `/admin/keys/issue` — the
    same five steps, not a copy. A key minted straight at the gateway leaves the ledger's
    recorded token hash pointing at a key that no longer exists and every later budget
    change fails silently. `actor` is the user, `principal` is the user; as with
    `/portal/api/keys/rotate` there is no argument here that can name somebody else.
    """
    obj = object_name(user, name)
    model = (model or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    if model not in allowed_models():
        raise HTTPException(
            400,
            f"unknown model {model!r}. A model name from a request body ends up verbatim "
            "in a pod spec, so it is checked against the deployment's list rather than "
            "passed through.",
        )
    # The Agents-pillar type (Contract A). Default is the incumbent `hermes`; an unknown
    # value is refused rather than passed through for the same reason `model` is — it ends
    # up verbatim in a pod label, and a per-type console/model selector trusts it.
    agent_type = (agent_type or DEFAULT_AGENT_TYPE).strip() or DEFAULT_AGENT_TYPE
    if agent_type not in AGENT_TYPES:
        raise HTTPException(
            400,
            f"unknown agent type {agent_type!r}. The Agents pillar carries "
            f"{', '.join(AGENT_TYPES)}; `opencode` is the Code pillar, not an agent type.",
        )

    async with _client() as client:
        existing = await _get(client, "apps/v1", "Deployment", obj)
        if existing is not None and _owner_of(existing) != user:
            # The collision. Refusing is the whole point: applying over it would hand this
            # caller another person's Deployment, PVC and running session.
            raise HTTPException(
                403,
                f"the object name {obj!r} already belongs to another user's agent. Agent "
                "names are joined to your username with a hyphen, so a name ending or "
                "starting with one can collide; choose a different name.",
            )
        if existing is not None:
            raise HTTPException(409, f"you already have an agent called {name!r}")

        # Mint the virtual key BEFORE the pod exists, so the agent never starts holding
        # -055's sentinel and 401ing with nothing on screen to say why. Type-agnostic: both
        # pillars run on the integrated <user>::agents/<name> key (agents-surface Contract
        # 1/3, one bill).
        issued = await issuance.issue(user, gateway.agent_surface(name), actor=user)
        api_key = issued["key"]
        keysum = hashlib.sha256(api_key.encode()).hexdigest()[:16]

        if agent_type == "hermes":
            await _provision_hermes(client, user, name, obj, model, api_key, keysum)
        else:
            # openclaw has no provisioner yet (enterpriseaiframework-ff7). Until it lands,
            # the interim path is the opencode render + type stamp that -5c9 established —
            # NOT the default (hermes is), so the Code-pillar template is only reached by an
            # explicit non-default type, exactly as the design record permits temporarily.
            await _provision_opencode_interim(
                client, user, name, obj, model, agent_type, api_key, keysum)

    await db.audit(user, "agent.create", f"{user}/{name}",
                   surface=gateway.agent_surface(name), alias=issued["key_alias"],
                   agent_type=agent_type)
    return {
        "name": name,
        "surface": gateway.agent_surface(name),
        "status": STARTING,
        "type": agent_type,
        "alias": issued["key_alias"],
        "console_url": console_url(name),
    }


def allowed_models() -> tuple[str, ...]:
    """Models a user may pick for a new agent. Deployment configuration, not a request field."""
    configured = os.environ.get("AGENT_MODELS", "")
    models = tuple(m.strip() for m in configured.split(",") if m.strip())
    return models or (DEFAULT_MODEL,)


# ---------------------------------------------------------------- stop / start


async def scale(user: str, name: str, replicas: int) -> dict:
    """Contract 2's running<->stopped transitions. The PVC is never touched by either.

    `stopped` is `replicas: 0`: the pod terminates, opencode's session is already
    checkpointed to the PVC's sqlite, and the -914 meter freezes because there is no pod
    to sample — not because anything told it to stop counting. `stopped -> running` is
    `replicas: 1` and resumes the SAME agent from the same volume.
    """
    async with _client() as client:
        await _owned_deployment(client, user, name)
        await _patch(client, "apps/v1", "Deployment", object_name(user, name),
                     {"spec": {"replicas": replicas}})
    await db.audit(user, "agent.stop" if replicas == 0 else "agent.start",
                   f"{user}/{name}", replicas=replicas)
    return {"name": name, "status": STOPPED if replicas == 0 else STARTING}


# ---------------------------------------------------------------- delete


async def delete(user: str, name: str) -> dict:
    """Contract 2's `deleted` transition, including the step that makes it irreversible.

    Order matters and the record spells it out: workload first, then the PVC, and the PVC
    deletion is CONFIRMED before this reports success. A half-deleted agent that left a
    Secret or a volume behind would be a spendable credential and a resident 5Gi with no
    owner and nothing rendering them.

    The virtual key is revoked in the same call. Deleting the pod without revoking leaves
    `<user>::agents/<name>` live and spendable at the gateway with nothing using it —
    exactly the state provision-agent.sh refuses to create when it declines to switch an
    integrated agent to BYO. A user pressing Delete must not be able to leave one behind.
    """
    async with _client() as client:
        await _owned_deployment(client, user, name)
        obj = object_name(user, name)

        removed = []
        for api_version, kind, target in (
            ("apps/v1", "Deployment", obj),
            ("v1", "Service", obj),
            # The hermes agent's first-boot seed ConfigMap (agents-gateway-console.md B). A
            # delete that left it behind would leak an orphan; a no-op for an opencode agent,
            # which has none. The :9119 NetworkPolicy is namespace-wide (66-agent-console-
            # common.yaml), not per-agent, so there is nothing agent-scoped to delete.
            ("v1", "ConfigMap", f"{obj}-config"),
            ("v1", "Secret", f"{obj}-key"),
            ("v1", "Secret", f"{obj}-byo"),
            # The connector credentials, for exactly the reason the virtual key is
            # revoked below: a delete that left `agent-alice-bot-slack` behind would leave
            # a live Slack bot token — the tenant's own, with whatever scopes they granted
            # it — sitting in the namespace with nothing using it and nothing rendering
            # it. Before this item nothing could write them from the portal, so nothing
            # had noticed; now that a user can create one from the browser, a user
            # pressing Delete must not be able to leave one behind.
            *(("v1", "Secret", CONNECTORS[k].secret_name(obj)) for k in sorted(CONNECTORS)),
        ):
            if await _delete(client, api_version, kind, target):
                removed.append(f"{kind}/{target}")

        # The point of no return, and the one step that is verified rather than assumed.
        if await _delete(client, "v1", "PersistentVolumeClaim", obj):
            removed.append(f"PersistentVolumeClaim/{obj}")
        still_there = await _get(client, "v1", "PersistentVolumeClaim", obj)

    alias = gateway.agent_key_alias(user, name)
    await gateway.delete_by_aliases([alias], missing_ok=True)
    pool = await db.pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE virtual_key SET status = 'revoked', revoked_at = now() "
            "WHERE key_alias = $1", alias,
        )
    await db.audit(user, "agent.delete", f"{user}/{name}", alias=alias,
                   removed=removed)

    return {
        "name": name,
        "deleted": True,
        "removed": removed,
        "key_revoked": alias,
        # A PVC with a finalizer still attached to a terminating pod reports as
        # Terminating for a few seconds. Saying so is honest; claiming the volume is gone
        # when it is not is how a "deleted" agent comes back on the next list.
        "volume_terminating": still_there is not None,
    }
