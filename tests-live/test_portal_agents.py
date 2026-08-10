"""A signed-in person creates, stops and deletes a real agent from the portal.

Run against the live k3s cluster:  pytest tests-live/test_portal_agents.py

This is Contract 2 of docs/design/records/agents-surface.md driven through the surface a
user actually has — `/portal/api/agents` — rather than through an operator's kubeconfig.
Until this landed the only way to get an agent was `deploy/bin/provision-agent.sh`, which
needs a kubeconfig, which a camper does not have and must never have.

WHERE EVERY EXPECTED VALUE COMES FROM

Not from the code under test, in any of the four claims that matter:

  * "it was created and it is running" is read with `kubectl get pod`, against the object
    names spelled out literally in this file rather than recomputed from `app/agents.py`'s
    own helpers. If the module renamed its objects, this test fails rather than following
    it;
  * "stopped means no pod" is `kubectl get deploy -o jsonpath={.spec.replicas}` and an
    empty pod list, and the FREEZE is sampled twice across a delay through the -914 meter's
    own forced collection — not by patching a clock;
  * "deleted means gone" is `kubectl get` returning nothing for all five object kinds,
    including the PVC, which Contract 2 calls the point of no return;
  * the cross-user refusal is driven by a SECOND REAL IDENTITY through the same loopback
    hop oauth2-proxy authenticates on, and the proof that it was refused is that the FIRST
    user's Deployment is still there afterwards with its replicas untouched — read with
    kubectl, not from a status code alone.

THE SECOND IDENTITY IS NOT A MOCK. `require_user` derives the name from the header the
sidecar sets, honoured only from 127.0.0.1 inside the control-plane pod. Driving it from
inside that pod with a different username is exactly what the sidecar does when a different
person signs in; it is the same technique tests-live/test_agent_usage.py uses to read one
user's own spend. There is no code path here that hands an endpoint an identity it did not
authenticate.

The throwaway agent is `agent-baron-portal627`. `baron` is used because an INTEGRATED agent
needs a real principal to mint a virtual key for, exactly as tests-live/test_agent_usage.py
does. The namespace runs the camp fixtures ws-baron / ws-claire / ws-student and this file
never touches them; teardown removes every `agent-baron-portal627*` object and the virtual
key, and runs even when a test fails.
"""

import json
import subprocess
import time

import pytest

NS = "enterprise-ai"
USER = "baron"
OTHER = "claire"
NAME = "portal627"

# Spelled out, not computed. See the docstring: recomputing these from app/agents.py would
# make this test follow a rename instead of catching one.
OBJ = f"agent-{USER}-{NAME}"
ALIAS = f"{USER}::agents/{NAME}"

READY_TIMEOUT_S = 420
# Long enough that a real collector tick lands inside it. The freeze window would otherwise
# prove only that nothing sampled, which is not the same claim.
FREEZE_WINDOW_S = 25


def _run(*args, check=True, timeout=700):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                          check=check)


def _kubectl(*args, check=True, timeout=300) -> str:
    return _run("kubectl", "-n", NS, *args, check=check, timeout=timeout).stdout


def _control_plane_pod() -> str:
    return _kubectl("get", "pod", "-l", "app=control-plane",
                    "-o", "jsonpath={.items[0].metadata.name}").strip()


def _in_control_plane(script: str, timeout=300) -> str:
    return _run("kubectl", "-n", NS, "exec", _control_plane_pod(), "-c", "control-plane",
                "--", "python3", "-c", script, timeout=timeout).stdout


_PORTAL_CALL = """
import json, httpx
r = httpx.request({method!r}, "http://127.0.0.1:8000{path}",
                  headers={{"x-auth-request-preferred-username": {user!r}}},
                  json={body!r}, timeout=180)
print(json.dumps({{"status": r.status_code, "body": r.json()}}))
"""


def portal(user: str, path: str, method: str = "GET", body=None) -> dict:
    """One portal call as `user`, over the loopback path oauth2-proxy authenticates on.

    THE identity seam. `require_user` honours the header only from 127.0.0.1, which inside
    this pod is where the sidecar's requests come from and nowhere else. Nothing here can
    name an owner: the endpoints take it from this header and never from the body or path.
    """
    out = _in_control_plane(
        _PORTAL_CALL.format(method=method, path=path, user=user, body=body or {})
    )
    return json.loads(out)


def _exists(kind: str, name: str) -> bool:
    proc = _run("kubectl", "-n", NS, "get", kind, name, "-o", "name", check=False)
    return proc.returncode == 0 and bool(proc.stdout.strip())


def _replicas() -> int:
    out = _kubectl("get", "deploy", OBJ, "-o", "jsonpath={.spec.replicas}").strip()
    return int(out or -1)


def _agent_pods() -> list[str]:
    out = _kubectl(
        "get", "pod",
        "-l", f"agent.enterprise-ai/user={USER},agent.enterprise-ai/name={NAME}",
        "-o", "jsonpath={.items[*].metadata.name}",
    ).strip()
    return out.split() if out else []


def _wait_pod_running(timeout_s=READY_TIMEOUT_S) -> str:
    deadline = time.time() + timeout_s
    last = ""
    while time.time() < deadline:
        out = _kubectl(
            "get", "pod",
            "-l", f"agent.enterprise-ai/user={USER},agent.enterprise-ai/name={NAME}",
            "-o", "jsonpath={.items[0].status.phase}", check=False,
        ).strip()
        last = out
        if out == "Running":
            return out
        time.sleep(4)
    raise AssertionError(
        f"{OBJ} never reached Running within {timeout_s}s (last phase {last!r}). "
        f"kubectl describe: {_kubectl('describe', 'deploy', OBJ, check=False)[-2000:]}"
    )


def _wait_gone(kind: str, name: str, timeout_s=180):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if not _exists(kind, name):
            return
        time.sleep(3)
    raise AssertionError(f"{kind}/{name} still exists {timeout_s}s after delete")


def _admin(path: str, method: str = "get") -> dict:
    script = (
        "import json, os, httpx\n"
        f"r = httpx.{method}('http://127.0.0.1:8000{path}',\n"
        "    headers={'Authorization': 'Bearer ' + os.environ['CONTROL_PLANE_ADMIN_TOKEN']},\n"
        "    timeout=180)\n"
        "r.raise_for_status()\n"
        "print(json.dumps(r.json()))\n"
    )
    return json.loads(_in_control_plane(script))


def _resident_seconds() -> float:
    """This agent's accrued resident time, straight out of the -914 ledger."""
    rows = _admin(f"/admin/agents/usage?username={USER}")["agents"]
    mine = [r for r in rows if r["agent"] == NAME]
    if not mine:
        return -1.0
    return float(mine[0]["resident_seconds"])


def _teardown():
    """Leave nothing. The camp runs on this namespace in hours."""
    for kind, name in (("deploy", OBJ), ("svc", OBJ),
                       ("secret", f"{OBJ}-key"), ("secret", f"{OBJ}-byo"),
                       ("pvc", OBJ)):
        _run("kubectl", "-n", NS, "delete", kind, name, "--ignore-not-found",
             "--wait=false", check=False)
    # Block on the PVC: a finalizer still terminating would make the next run's create
    # bind to a volume that is on its way out.
    deadline = time.time() + 180
    while time.time() < deadline and _exists("pvc", OBJ):
        time.sleep(3)


@pytest.fixture(scope="module", autouse=True)
def agent_surface_is_deployed():
    """Refuse to run — loudly — against a control plane that predates this surface.

    A 404 from `/portal/api/agents` would otherwise read as "the user has no agents",
    which is exactly the shape of a passing test against a feature that is not deployed.
    """
    probe = portal(USER, "/portal/api/agents")
    assert probe["status"] == 200, (
        f"/portal/api/agents answered {probe['status']} — the running control-plane image "
        f"does not carry enterpriseaiframework-627. Build and roll it before running this "
        f"file. Body: {probe['body']}"
    )
    _teardown()
    yield
    _teardown()


# ---------------------------------------------------------------- the lifecycle


def test_a_signed_in_user_creates_a_real_agent_that_reaches_running():
    created = portal(USER, "/portal/api/agents", "POST", {"name": NAME})
    assert created["status"] == 201, created

    _wait_pod_running()

    # Ground truth, from the cluster and not from the endpoint that just claimed it.
    assert _exists("deploy", OBJ), f"no Deployment {OBJ} after a 201"
    assert _exists("svc", OBJ)
    assert _exists("pvc", OBJ)
    assert _exists("secret", f"{OBJ}-key")

    labels = json.loads(_kubectl("get", "deploy", OBJ, "-o", "json"))["metadata"]["labels"]
    assert labels["agent.enterprise-ai/user"] == USER, (
        "the owner label is what every authorisation check in app/agents.py reads; a wrong "
        "one means the agent belongs to nobody and the guard cannot work"
    )
    assert labels["agent.enterprise-ai/name"] == NAME

    # The image is the one the Code surface is ACTUALLY running, read off a live workspace
    # pod — the reason tests-live/test_agent_resident.py gives for never pasting a tag.
    ws_image = _kubectl(
        "get", "pod", "-l", "app.kubernetes.io/component=workspace",
        "-o", 'jsonpath={.items[0].spec.containers[?(@.name=="ttyd")].image}',
    ).strip()
    agent_image = _kubectl(
        "get", "deploy", OBJ, "-o", "jsonpath={.spec.template.spec.containers[0].image}",
    ).strip()
    assert agent_image == ws_image, (
        f"the agent runs {agent_image} but the Code surface runs {ws_image}; Contract 6 "
        "says the agent reuses the workspace artefact rather than building its own"
    )

    # The minted key reached the pod, and it is not -055's sentinel — an agent that starts
    # holding the sentinel 401s on its first request with nothing on screen to say why.
    key = _run("bash", "-c",
               f"kubectl -n {NS} get secret {OBJ}-key -o jsonpath='{{.data.OPENAI_API_KEY}}'"
               " | base64 -d").stdout
    assert key and key != "unset-pending-enterpriseaiframework-39d", (
        "the agent was created without a usable virtual key"
    )

    listed = portal(USER, "/portal/api/agents")["body"]["agents"]
    mine = [a for a in listed if a["name"] == NAME]
    assert mine, f"the created agent is not in its owner's own list: {listed}"
    assert mine[0]["status"] == "running", mine[0]
    assert mine[0]["surface"] == f"agents/{NAME}"
    assert mine[0]["console_url"] == f"/agents/{NAME}/"
    # Both metering dimensions present and kept apart: dollars for inference, quantities
    # for residency. Never added together — owned compute has no price (-914).
    assert "inference" in mine[0] and "usage" in mine[0]
    assert mine[0]["inference"]["on_ledger"] is True


def test_stopping_scales_to_zero_keeps_the_volume_and_freezes_the_meter():
    """Contract 2's `stopped`, and the claim that makes it worth having.

    Stopped costs nothing because there is no pod — not because a rate was set to zero. So
    the assertion is not "the meter reports zero", it is "the meter's total is the same
    number after a real interval", sampled through the -914 collector's own forced run.
    """
    _admin("/admin/agents/usage/collect", method="post")
    before = _resident_seconds()
    assert before > 0, "the agent accrued no resident time while it was running"

    stopped = portal(USER, f"/portal/api/agents/{NAME}/stop", "POST")
    assert stopped["status"] == 200, stopped

    deadline = time.time() + 180
    while time.time() < deadline and _agent_pods():
        time.sleep(3)
    assert _replicas() == 0, f"stop left replicas at {_replicas()}"
    assert _agent_pods() == [], "a pod survived the scale to zero"
    assert _exists("pvc", OBJ), (
        "stopping destroyed the volume — Contract 2 promises stop keeps state and only "
        "delete destroys it"
    )
    assert _exists("secret", f"{OBJ}-key"), "stopping destroyed the agent's key"

    assert portal(USER, "/portal/api/agents")["body"]["agents"][0]["status"] == "stopped"

    _admin("/admin/agents/usage/collect", method="post")
    frozen = _resident_seconds()
    time.sleep(FREEZE_WINDOW_S)
    _admin("/admin/agents/usage/collect", method="post")
    still = _resident_seconds()
    assert still == frozen, (
        f"resident time moved from {frozen} to {still} across {FREEZE_WINDOW_S}s with no "
        "pod running — a stopped agent is accruing usage it cannot possibly be consuming"
    )


def test_starting_a_stopped_agent_resumes_the_same_one_from_the_same_volume():
    pvc_uid = _kubectl("get", "pvc", OBJ, "-o", "jsonpath={.metadata.uid}").strip()
    started = portal(USER, f"/portal/api/agents/{NAME}/start", "POST")
    assert started["status"] == 200, started
    _wait_pod_running()
    assert _replicas() == 1
    assert _kubectl("get", "pvc", OBJ, "-o", "jsonpath={.metadata.uid}").strip() == pvc_uid, (
        "start bound a NEW volume; the agent's work and its opencode session live on the "
        "old one and stop->start must resume the same agent, not create a fresh one"
    )


# ---------------------------------------------------------------- the abuse paths


def test_a_second_real_identity_cannot_see_stop_or_delete_the_first_users_agent():
    """The isolation boundary, driven by a different person through the same front door.

    RBAC cannot help here: the control-plane ServiceAccount holds namespaced write on
    Deployments and can delete this object. The only thing stopping `claire` is the owner
    check in app/agents.py, so this is the test that stands between one camper and every
    other camper's agent.
    """
    assert _replicas() == 1, "precondition: baron's agent is running"

    listed = portal(OTHER, "/portal/api/agents")["body"]["agents"]
    assert all(a["name"] != NAME for a in listed), (
        f"{OTHER} can see {USER}'s agent in their own list: {listed}"
    )

    for method, path in (
        ("POST", f"/portal/api/agents/{NAME}/stop"),
        ("POST", f"/portal/api/agents/{NAME}/start"),
        ("DELETE", f"/portal/api/agents/{NAME}"),
    ):
        resp = portal(OTHER, path, method)
        assert resp["status"] == 404, (
            f"{method} {path} as {OTHER} returned {resp['status']} — one user reached "
            f"another user's agent by name. Body: {resp['body']}"
        )

    # The status code is not the proof. This is.
    assert _replicas() == 1, f"{OTHER}'s request changed {USER}'s agent's replicas"
    assert _exists("deploy", OBJ) and _exists("pvc", OBJ), (
        f"{OTHER}'s request destroyed {USER}'s agent"
    )
    assert _agent_pods(), f"{OTHER}'s request terminated {USER}'s running pod"


def test_the_identity_headers_are_ignored_from_anywhere_but_the_sidecar():
    """The same four endpoints, reached from another pod with a perfect header.

    Every pod in the namespace can open a socket to the control-plane Service. If the
    agent endpoints honoured the header from there, any workload in the namespace could
    create and destroy anybody's agent.
    """
    script = (
        "import json, httpx\n"
        "out = {}\n"
        "for method, path in (('GET','/portal/api/agents'),"
        " ('POST','/portal/api/agents'),"
        f" ('POST','/portal/api/agents/{NAME}/stop'),"
        f" ('DELETE','/portal/api/agents/{NAME}')):\n"
        "    r = httpx.request(method, 'http://control-plane:8000' + path,\n"
        "        headers={'x-auth-request-preferred-username': 'baron'},\n"
        f"        json={{'name': '{NAME}'}}, timeout=60)\n"
        "    out[method + ' ' + path] = r.status_code\n"
        "print(json.dumps(out))\n"
    )
    # From the gateway pod: a real other pod on the pod network, not loopback.
    pod = _kubectl("get", "pod", "-l", "app=gateway",
                   "-o", "jsonpath={.items[0].metadata.name}").strip()
    raw = _run("kubectl", "-n", NS, "exec", pod, "--", "python3", "-c", script).stdout
    statuses = json.loads(raw)
    assert set(statuses.values()) == {403}, (
        f"a forged identity header from another pod was honoured: {statuses}"
    )
    assert _replicas() == 1, "the off-pod request changed the agent"


# ---------------------------------------------------------------- delete


def test_deleting_removes_every_object_including_the_volume_and_the_key():
    resp = portal(USER, f"/portal/api/agents/{NAME}", "DELETE")
    assert resp["status"] == 200, resp
    assert resp["body"]["deleted"] is True

    for kind, name in (("deploy", OBJ), ("svc", OBJ), ("secret", f"{OBJ}-key")):
        _wait_gone(kind, name)
    # The point of no return, and the one Contract 2 says must be confirmed.
    _wait_gone("pvc", OBJ)
    assert _agent_pods() == [], "a pod outlived the deletion of its Deployment"

    assert portal(USER, "/portal/api/agents")["body"]["agents"] == [], (
        "a deleted agent is still listed"
    )

    # The virtual key must not outlive the agent: a live `baron::agents/portal627` at the
    # gateway with nothing using it is a spendable credential nobody is watching.
    keys = _admin("/admin/keys")
    rows = keys["keys"] if isinstance(keys, dict) else keys
    live = [k for k in rows
            if k.get("key_alias") == ALIAS and k.get("status") == "active"]
    assert not live, f"the agent's virtual key survived its deletion: {live}"


# ---------------------------------------------------------------- the other two tabs


def test_the_chat_and_code_tabs_are_unchanged_on_the_deployed_page():
    """Additive, asserted against what the RUNNING control plane serves.

    Not against this checkout's files — control-plane/tests/test_portal_agents.py does
    that. This is the page a camper's browser will actually receive tomorrow.
    """
    script = (
        "import httpx\n"
        "h = {'x-auth-request-preferred-username': 'baron'}\n"
        "page = httpx.get('http://127.0.0.1:8000/portal/', headers=h, timeout=60).text\n"
        "js = httpx.get('http://127.0.0.1:8000/portal/static/app.js', headers=h,"
        " timeout=60).text\n"
        "print(repr((page, js)))\n"
    )
    page, js = eval(_in_control_plane(script))  # noqa: S307 - our own repr, from our own pod

    for marker in ('id="tab-chat"', 'id="tab-code"', 'id="view-chat"', 'id="view-code"',
                   'id="frame-chat"', 'id="frame-code"'):
        assert marker in page, f"the deployed page lost {marker}"
    assert 'id="tab-agents"' in page, "the Agents tab is not on the deployed page"
    assert '"/workshop/"' in js, (
        "the deployed Code tab no longer points at the workshop proxy on this origin"
    )


SA = "system:serviceaccount:enterprise-ai:control-plane"


def _can_i(verb_resource: str, *extra) -> bool:
    proc = _run("kubectl", "auth", "can-i", *verb_resource.split(), *extra,
                f"--as={SA}", check=False)
    return proc.stdout.strip() == "yes"


def test_the_write_grant_is_exactly_as_narrow_as_it_claims_to_be():
    """The RBAC boundary, asked of the API server rather than read off the YAML.

    `deploy/k8s/39-control-plane-rbac.yaml` argues at length that the write grant is
    narrow. That argument is worth nothing unless something checks the EFFECTIVE
    permissions, which are the union of every binding on this cluster — including ones
    this repository does not contain. `kubectl auth can-i` asks the authorizer, which is
    the only authority on the answer.

    The refusals below are the ones that matter, each with the escalation it forecloses:
    rolebindings and `escalate` are how a component that can create a Deployment becomes
    cluster-admin; pods/exec is a shell in somebody else's container; serviceaccounts and
    networkpolicies are the two objects that make an agent pod safe to run unattended.
    """
    for granted in ("create deployments", "patch deployments", "delete deployments",
                    "create services", "create persistentvolumeclaims",
                    "delete persistentvolumeclaims", "create secrets", "delete secrets",
                    "create configmaps", "list pods"):
        assert _can_i(granted, "-n", NS), (
            f"the control plane cannot `{granted}` in {NS}; the Agents tab cannot work. "
            "Apply deploy/k8s/39-control-plane-rbac.yaml."
        )

    for refused in ("create rolebindings", "create roles", "escalate roles",
                    "bind roles", "create pods/exec", "create pods/portforward",
                    "patch serviceaccounts", "patch networkpolicies",
                    "delete pods", "create jobs"):
        assert not _can_i(refused, "-n", NS), (
            f"the control-plane ServiceAccount can `{refused}` in {NS}. That is wider "
            "than enterpriseaiframework-627 asked for and wider than the surface needs; "
            "some binding on this cluster grants it."
        )

    # Namespaced means namespaced. A cluster-scoped write would put every namespace on
    # this cluster — the GPU training ones included — behind one owner check in Python.
    for cluster_wide in ("create deployments", "delete secrets", "create configmaps",
                         "delete persistentvolumeclaims"):
        assert not _can_i(cluster_wide, "--all-namespaces"), (
            f"the control-plane ServiceAccount can `{cluster_wide}` in EVERY namespace"
        )
    assert not _can_i("create namespaces"), "the control plane can create namespaces"
    assert not _can_i("delete nodes"), "the control plane can delete nodes"


def test_the_camp_fixtures_and_every_platform_pod_are_untouched():
    """The constraint this branch runs under, checked rather than promised.

    The camp runs on ws-baron / ws-claire / ws-student. Creating and destroying agents in
    this namespace must not have disturbed one of them.
    """
    out = _kubectl("get", "pod", "-o",
                   "jsonpath={range .items[*]}{.metadata.name}={.status.phase}{'\\n'}{end}")
    bad = [line for line in out.splitlines()
           if line and not line.endswith("=Running") and not line.endswith("=Succeeded")]
    assert not bad, f"pods are not healthy after this run: {bad}"
    for ws in ("ws-baron", "ws-claire", "ws-student"):
        assert _exists("deploy", ws), f"{ws} is gone"
