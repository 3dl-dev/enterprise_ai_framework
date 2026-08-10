"""An agent is a RESIDENT daemon, not a process a console spawns.

Run against the live k3s cluster:  pytest tests-live/test_agent_resident.py

This is the measurement behind Contract 2 of docs/design/records/agents-surface.md, and
the reason the Agents surface exists as a separate surface at all. Finding 43 recorded
what the Code/workspace surface does: ttyd spawns a fresh `opencode` for EVERY websocket,
and it dies when the browser disconnects — a 55%-CPU, 712-MB cold boot on every reconnect.
That is right for Code, where a person is looking at the agent. It is exactly wrong for an
Agent, whose entire value is being away from it.

So the claim under test is a negative one and it cannot be proven by opening a console:
the daemon must be up and holding its session with NOTHING attached. Nothing in this file
connects a console — there is no ttyd in the pod and no port-forward is opened — and the
process is sampled across a window to show it persists anyway.

Every expected value here comes from the cluster or from the repository, never from the
code under test: the image is read off the RUNNING workspace pods, the object names are
spelled out literally rather than recomputed from the provisioner's own variables, and the
PVC marker is a uuid this test invents and then reads back through a different pod.

The throwaway agent is `agent-swtest055-probe`. The namespace runs the camp fixtures
ws-baron / ws-claire / ws-student; teardown removes every `agent-swtest055-*` object and
runs even when a test fails.
"""

import json
import subprocess
import time
import uuid

import pytest

NS = "enterprise-ai"
USER = "swtest055"
NAME = "probe"
OBJ = f"agent-{USER}-{NAME}"


def _run(*args, check=True, timeout=700):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=check)


def _kubectl(*args, check=True, timeout=300) -> str:
    return _run("kubectl", "-n", NS, *args, check=check, timeout=timeout).stdout


def _workspace_image() -> str:
    """Ground truth for the image: what the Code surface is ACTUALLY running right now.

    Not what this checkout's HEAD would compute, and not a tag pasted into this file. The
    deployed tag on a cluster is routinely not the current commit, and a provisioner that
    reused "the same image" while naming a tag the registry has never seen would fail as an
    ImagePullBackOff that looks like a cluster problem rather than a wrong assumption.
    """
    image = _kubectl(
        "get", "pod", "-l", "app.kubernetes.io/component=workspace",
        "-o", 'jsonpath={.items[0].spec.containers[?(@.name=="ttyd")].image}',
    ).strip()
    assert image, "no running workspace pod to read the image from"
    return image


def _pod_name() -> str:
    return _kubectl(
        "get", "pod", "-l", f"agent.enterprise-ai/name={NAME},agent.enterprise-ai/user={USER}",
        "-o", "jsonpath={.items[0].metadata.name}",
    ).strip()


def _pod_json() -> dict:
    out = _kubectl(
        "get", "pod", "-l", f"agent.enterprise-ai/name={NAME},agent.enterprise-ai/user={USER}",
        "-o", "json",
    )
    items = json.loads(out)["items"]
    assert items, f"no pod for {OBJ}"
    return items[0]


def _exec(script: str, check=True) -> subprocess.CompletedProcess:
    return _run("kubectl", "-n", NS, "exec", _pod_name(), "--",
                "bash", "-c", script, check=check)


def _wait_running(timeout_s=300) -> str:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            pod = _pod_json()
        except AssertionError:
            time.sleep(3)
            continue
        if pod["status"].get("phase") == "Running" and all(
            c.get("ready") for c in pod["status"].get("containerStatuses", [])
        ):
            return pod["metadata"]["name"]
        time.sleep(3)
    raise AssertionError(f"{OBJ} pod never became Ready within {timeout_s}s")


def _teardown() -> None:
    """Delete every throwaway object. The camp runs here in hours; leave nothing.

    Deletion order mirrors Contract 2's `deleted` transition: workload first, then the PVC,
    which is the point of no return. `--ignore-not-found` so a partial provision still
    tears down cleanly.

    It BLOCKS until the PVC is really gone, and that is not tidiness. This same function is
    the pre-clean at the start of the fixture, and a PVC still Terminating (its finalizer
    held until the last pod releases it) when the provisioner re-applies one under the same
    name is a race whose symptom is a pod stuck in ContainerCreating — which reads as "the
    agent surface is broken" rather than "the previous run had not finished dying".
    """
    _kubectl("delete", "deployment", OBJ, "--ignore-not-found", "--wait=true",
             check=False, timeout=300)
    _kubectl("delete", "service", OBJ, "--ignore-not-found", check=False)
    _kubectl("delete", "secret", f"{OBJ}-key", "--ignore-not-found", check=False)
    _kubectl("delete", "pvc", OBJ, "--ignore-not-found", "--wait=true",
             check=False, timeout=300)

    deadline = time.time() + 180
    while time.time() < deadline:
        remaining = _kubectl("get", "pvc", OBJ, "--ignore-not-found",
                             "-o", "name", check=False).strip()
        if not remaining:
            return
        time.sleep(2)
    raise AssertionError(f"pvc/{OBJ} is still present 180s after delete")


@pytest.fixture(scope="module", autouse=True)
def provisioned_agent():
    """Provision the throwaway agent with the real script, and always clean up.

    `AGENT_IMAGE` is passed rather than letting the script default to
    `<registry>/enterprise-ai-workspace:<git HEAD>`, because HEAD in a test worktree has
    never been built or pushed. The value comes from the live workspace pods, so this is
    still a measurement of "the agent reuses the Code surface's image", not an assumption.
    """
    _teardown()
    try:
        proc = _run(
            "env", f"AGENT_IMAGE={_workspace_image()}",
            "deploy/bin/provision-agent.sh", USER, NAME,
            check=False, timeout=900,
        )
        assert proc.returncode == 0, (
            f"provision-agent.sh failed ({proc.returncode}):\n{proc.stdout}\n{proc.stderr}"
        )
        yield
    finally:
        _teardown()


def test_the_deployment_becomes_available_and_the_pod_runs():
    """created -> running, the first transition in Contract 2's lifecycle table."""
    _kubectl("rollout", "status", f"deployment/{OBJ}", "--timeout=600s")
    pod = _pod_json()
    assert pod["status"]["phase"] == "Running", pod["status"]
    conditions = {
        c["type"]: c["status"]
        for c in json.loads(_kubectl("get", "deployment", OBJ, "-o", "json"))["status"].get(
            "conditions", []
        )
    }
    assert conditions.get("Available") == "True", conditions


def test_the_agent_reuses_the_code_surfaces_image_and_is_named_per_contract_one():
    """Contract 1's names and the image reuse, read back off the object that exists."""
    pod = _pod_json()
    container = pod["spec"]["containers"][0]
    assert container["image"] == _workspace_image()
    assert pod["metadata"]["labels"]["agent.enterprise-ai/user"] == USER
    assert pod["metadata"]["labels"]["agent.enterprise-ai/name"] == NAME
    # The component label must NOT be `workspace`, or the workspace NetworkPolicy and the
    # per-user workspace Services would start selecting agent pods.
    assert pod["metadata"]["labels"]["app.kubernetes.io/component"] == "agent"
    assert container["volumeMounts"], container
    claims = {
        v["persistentVolumeClaim"]["claimName"]
        for v in pod["spec"]["volumes"]
        if "persistentVolumeClaim" in v
    }
    assert claims == {OBJ}, claims


def test_opencode_serve_is_the_pods_own_process_and_no_ttyd_exists():
    """The residency invariant, stated as the two halves that distinguish it from Code.

    Positive: `opencode serve` is a direct child of PID 1, i.e. it is what the container
    runs, not something a connection started.
    Negative: there is no ttyd in this pod at all — so there is no mechanism by which a
    console could be spawning it, which is what makes the persistence measured below mean
    residency rather than "somebody happened to be connected".
    """
    ps = _exec("ps -eo pid,ppid,args").stdout
    serve = [line for line in ps.splitlines() if "opencode serve" in line]
    assert len(serve) == 1, f"expected exactly one resident opencode serve:\n{ps}"
    assert int(serve[0].split()[1]) == 1, f"opencode serve is not a child of PID 1:\n{ps}"
    assert "ttyd" not in ps, (
        f"ttyd is running in an agent pod. That is the Code surface's spawn-per-websocket "
        f"model (finding 43) and it is exactly what an Agent must not be:\n{ps}"
    )


def test_the_daemon_serves_and_demands_a_credential():
    """Alive is not enough: the resident session must actually be being served.

    401 without the credential and 200 with it, measured from inside the pod against the
    daemon's own port. The password is the second lock behind the NetworkPolicy — a
    resident agent nobody is watching must not answer whatever reaches it.
    """
    password = _run(
        "bash", "-c",
        f"kubectl -n {NS} get secret {OBJ}-key -o jsonpath='{{.data.OPENCODE_SERVER_PASSWORD}}' | base64 -d",
    ).stdout.strip()
    assert password, "no OPENCODE_SERVER_PASSWORD in the agent's Secret"

    anon = _exec("curl -sS -m 10 -o /dev/null -w '%{http_code}' http://127.0.0.1:4096/app").stdout
    assert anon.strip() == "401", f"unauthenticated request to the daemon returned {anon}"
    authed = _exec(
        f"curl -sS -m 10 -u opencode:{password} -o /dev/null -w '%{{http_code}}' "
        "http://127.0.0.1:4096/app"
    ).stdout
    assert authed.strip() == "200", f"authenticated request to the daemon returned {authed}"


def test_the_daemon_stays_up_with_no_console_attached():
    """The measurement finding 43 would have failed.

    Nothing in this test opens a console, a websocket or a port-forward. The daemon is
    sampled twice across a window; the SAME pid must still be there, the container must
    not have restarted, and the process must not have been re-execed. On the Code surface
    the equivalent process would not exist at all between connections.
    """
    def sample():
        pid = _exec("pgrep -f 'opencode serve' | head -1").stdout.strip()
        started = _exec("ps -o lstart= -p $(pgrep -f 'opencode serve' | head -1)").stdout.strip()
        status = _pod_json()["status"]["containerStatuses"][0]
        return pid, started, status["restartCount"]

    first = sample()
    assert first[0], "no resident opencode serve process at the first sample"
    time.sleep(45)
    second = sample()

    assert second[0] == first[0], (
        f"the resident pid changed with nothing attached: {first[0]} -> {second[0]}. "
        "The daemon was restarted, so the session did not survive — which is the entire "
        "difference between an Agent and a workspace."
    )
    assert second[1] == first[1], f"process start time moved: {first[1]} -> {second[1]}"
    assert second[2] == first[2] == 0, f"container restarted: {first[2]} -> {second[2]}"


def test_the_agent_cannot_reach_anything_but_its_allowlist():
    """The isolation posture, measured from inside the pod rather than read off the YAML.

    An agent runs UNATTENDED next to a spendable key. The gateway is the one route out;
    the Kubernetes API, the control plane, Postgres and every workspace must be closed.
    """
    probe = (
        "python3 -c \"import socket,json;"
        "t={'gateway':('gateway',4000),'k8s-api':('10.43.0.1',443),"
        "'control-plane':('control-plane',8000),'postgres':('postgres',5432),"
        "'ws-student':('ws-student',7681)};o={}\n"
        "for k,(h,p) in t.items():\n"
        " s=socket.socket();s.settimeout(6)\n"
        " try:\n"
        "  s.connect((h,p));o[k]='open'\n"
        " except Exception as e:\n"
        "  o[k]=type(e).__name__\n"
        " finally:\n"
        "  s.close()\n"
        "print(json.dumps(o))\""
    )
    reached = json.loads(_exec(probe).stdout.strip().splitlines()[-1])
    assert reached["gateway"] == "open", reached
    for closed in ("k8s-api", "control-plane", "postgres", "ws-student"):
        assert reached[closed] != "open", (
            f"an agent pod reached {closed}: {reached}. Read the egress allowlist in "
            "deploy/k8s/63-agent-common.yaml — and note the CNI caveat recorded there."
        )


def test_pvc_state_survives_the_pod_being_destroyed():
    """`stopped` keeps state, and only `deleted` destroys it (Contract 2).

    A pod delete is the harshest form of the stopped -> running transition: the container
    is gone, and everything not on the PVC with it. The marker is a uuid this test
    generates, written through one pod and read back through a different one.
    """
    marker = uuid.uuid4().hex
    _exec(f"printf '%s' {marker} > /workspace/work/.resident-marker")
    old_pod = _pod_name()
    assert _exec("cat /workspace/work/.resident-marker").stdout.strip() == marker

    _kubectl("delete", "pod", old_pod, "--wait=true", timeout=300)
    new_pod = _wait_running()
    assert new_pod != old_pod, "the pod was not actually replaced"

    read_back = _exec("cat /workspace/work/.resident-marker").stdout.strip()
    assert read_back == marker, (
        f"the PVC did not carry the agent's state across a pod replacement: wrote {marker}, "
        f"read '{read_back}'. Without this, `stopped` is a delete and the lifecycle in "
        "Contract 2 is a fiction."
    )
    # And the daemon came back resident on the new pod, not waiting for a console.
    ps = _exec("ps -eo pid,ppid,args").stdout
    assert "opencode serve" in ps, ps
