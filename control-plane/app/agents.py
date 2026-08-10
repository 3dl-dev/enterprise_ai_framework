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

# The object set and the resident entrypoint, delivered to this pod as a ConfigMap because
# the control-plane image is built from `control-plane/` alone and these files live under
# `deploy/`. Rendering the SAME bytes provision-agent.sh renders is the point: a second
# copy of the Deployment shape in Python would drift from the template within a release,
# and the drift would be invisible until an agent came up subtly different from the one an
# operator provisioned by hand.
ASSETS_DIR = Path(os.environ.get("AGENT_ASSETS_DIR", "/etc/agent-assets"))

_REPO = Path(__file__).resolve().parents[2]
_ASSET_FALLBACK = {
    "64-agent.template.yaml": _REPO / "deploy" / "k8s" / "64-agent.template.yaml",
    "entrypoint.sh": _REPO / "deploy" / "agent" / "entrypoint.sh",
}

# What the pod's Secret holds before a real key is written into it. -055's sentinel,
# spelled identically so a grep for a 401 finds the same item from either direction.
KEY_SENTINEL = "unset-pending-enterpriseaiframework-39d"

# Status strings this module reports. `created` is deliberately distinct from `running`:
# replicas is 1 but no pod is Running yet, and calling that "running" is how a page comes
# to show a green dot next to an agent that is in ImagePullBackOff.
RUNNING, STARTING, STOPPED, UNKNOWN = "running", "starting", "stopped", "unknown"


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
            "replicas": (dep.get("spec") or {}).get("replicas", 0),
            "created_at": (dep.get("metadata") or {}).get("creationTimestamp", ""),
            # Where -0e7 will attach a console. The portal renders the entry now so the
            # surface is complete from a user's side the moment the proxy lands; the path
            # never carries the owner, which is Contract 1's rule for exactly this URL.
            "console_url": console_url(name),
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
        await _owned_deployment(client, user, name)
        secret = await _get(client, "v1", "Secret", f"{obj}-key")

    encoded = ((secret or {}).get("data") or {}).get("OPENCODE_SERVER_PASSWORD")
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
        "host": obj,
        "port": SERVE_PORT,
        "username": CONSOLE_BASIC_USER,
        "password": base64.b64decode(encoded).decode(),
    }


# ---------------------------------------------------------------- create


def render(user: str, name: str, *, image: str, model: str, api_base: str,
           model_source: str, key_secret: str, cfgsum: str, keysum: str) -> list[dict]:
    """The template, substituted exactly as provision-agent.sh substitutes it.

    Literal replacement of the same nine placeholders, then a YAML parse — not a
    hand-built object graph. The template is the design (its comments are the reasoning
    for every field in it) and this is a second renderer of it, not a second copy.
    """
    text = asset("64-agent.template.yaml")
    for placeholder, value in (
        ("__USER__", user), ("__NAME__", name), ("__IMAGE__", image),
        ("__MODEL__", model), ("__CFGSUM__", cfgsum), ("__KEYSUM__", keysum),
        ("__MODEL_SOURCE__", model_source), ("__API_BASE__", api_base),
        ("__KEY_SECRET__", key_secret),
    ):
        text = text.replace(placeholder, value)
    docs = [d for d in yaml.safe_load_all(text) if d]
    if not docs:
        raise HTTPException(500, "the agent template rendered to nothing")
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


def _secret_object(name: str, data: dict[str, str]) -> dict:
    return {
        "apiVersion": "v1", "kind": "Secret",
        "metadata": {"name": name, "namespace": namespace(), "labels": {
            "app.kubernetes.io/part-of": "enterprise-ai-framework",
            "app.kubernetes.io/component": "agent",
        }},
        "type": "Opaque",
        "data": {k: base64.b64encode(v.encode()).decode() for k, v in data.items()},
    }


async def create(user: str, name: str, *, model: str | None = None) -> dict:
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

        image = await _workspace_image(client)

        # The resident entrypoint, deployment-wide (one control plane). Applied here for
        # the same reason provision-agent.sh applies it: the pod mounts it by name, and an
        # agent must never be able to exist before the thing it runs does.
        entrypoint = asset("entrypoint.sh")
        cfgsum = hashlib.sha256(entrypoint.encode()).hexdigest()[:16]
        await _apply(client, {
            "apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": "agent-entrypoint", "namespace": namespace()},
            "data": {"entrypoint.sh": entrypoint},
        })

        issued = await issuance.issue(user, gateway.agent_surface(name), actor=user)
        api_key = issued["key"]
        keysum = hashlib.sha256(api_key.encode()).hexdigest()[:16]

        # HTTP Basic on the opencode server: entrypoint.sh refuses to start without it,
        # so an agent can never come up reachable and unauthenticated.
        password = secrets.token_urlsafe(24)
        await _apply(client, _secret_object(f"{obj}-key", {
            "OPENCODE_SERVER_PASSWORD": password,
            "OPENAI_API_KEY": api_key,
        }))

        for doc in render(
            user, name, image=image, model=model, api_base=GATEWAY_BASE,
            # Integrated only from the portal. BYO takes a provider credential that must
            # be handled set-once and never read back (Contract 4); accepting one through
            # a JSON body on a page is a different item's design, and the operator path
            # `provision-agent.sh --byo-key-file` is what does it today.
            model_source="integrated", key_secret=f"{obj}-key",
            cfgsum=cfgsum, keysum=keysum,
        ):
            await _apply(client, doc)

    await db.audit(user, "agent.create", f"{user}/{name}",
                   surface=gateway.agent_surface(name), alias=issued["key_alias"])
    return {
        "name": name,
        "surface": gateway.agent_surface(name),
        "status": STARTING,
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
            ("v1", "Secret", f"{obj}-key"),
            ("v1", "Secret", f"{obj}-byo"),
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
