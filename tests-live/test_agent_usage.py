"""Resident time and compute are metered per agent, from the real cluster, in real units.

Run against the live k3s cluster:  pytest tests-live/test_agent_usage.py

This is Contract 3(b) of docs/design/records/agents-surface.md — the second metering
dimension — measured rather than asserted. **Baron ruled that it meters USAGE, not cost**:
the hardware is owned and its cost is sunk, so what is proven here is a set of quantities
(hours, CPU-core-hours, megabytes) and the absence of any dollar figure anywhere near
them.

WHERE EVERY EXPECTED VALUE COMES FROM

Not from the code under test, in any of the four cases that matter:

  * resident time is checked against the pod's own `.status.startTime`, read with
    `kubectl` on a path that shares nothing with the control plane's collector;
  * the CPU counter is checked against an INDEPENDENT scrape of the same real cAdvisor
    endpoint, taken through the API server's `nodes/proxy` with the operator's kubeconfig
    — a different identity, a different route, the same ground truth. The control plane
    deliberately cannot use that route (see deploy/k8s/39-control-plane-rbac.yaml), which
    is what makes it independent rather than a second call to the same client;
  * the freeze is proven by STOPPING a real agent (`replicas: 0`, Contract 2) and sampling
    the ledger twice across a delay — not by monkeypatching a clock;
  * "the operator's bill did not move" is proven against the real `/admin/spend`, by an
    identity that holds whatever concurrent traffic the cluster is serving (see
    `test_the_inference_bill_is_unperturbed`).

The throwaway agent is `agent-baron-usage914`. `baron` is used because an INTEGRATED agent
needs a real principal to mint a virtual key for, exactly as tests-live/test_agent_model_api.py
does; teardown revokes that key and deletes its ledger row. The namespace runs the camp
fixtures ws-baron / ws-claire / ws-student and this file never touches them.
"""

import base64
import json
import subprocess
import time
from datetime import datetime, timezone

import pytest

NS = "enterprise-ai"
USER = "baron"
NAME = "usage914"
OBJ = f"agent-{USER}-{NAME}"
ALIAS = f"{USER}::agents/{NAME}"
SURFACE = f"agents/{NAME}"

# How long the "is it still moving?" and "is it really frozen?" windows are. Both are
# comfortably longer than the collector's own sample interval so that a real tick lands
# inside each of them; the frozen window would otherwise prove only that nothing sampled.
GROWTH_WINDOW_S = 12
FREEZE_WINDOW_S = 25


def _run(*args, check=True, timeout=700):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=check)


def _kubectl(*args, check=True, timeout=300) -> str:
    return _run("kubectl", "-n", NS, *args, check=check, timeout=timeout).stdout


def _secret(name: str, key: str) -> str:
    out = _kubectl("get", "secret", name, "-o", f"jsonpath={{.data.{key}}}")
    return base64.b64decode(out).decode()


def _control_plane_pod() -> str:
    return _kubectl("get", "pod", "-l", "app=control-plane",
                    "-o", "jsonpath={.items[0].metadata.name}").strip()


def _in_control_plane(script: str) -> str:
    """Run a snippet inside the control-plane container.

    Used for two things only: calling `/portal/api/*`, which is reachable ONLY from
    loopback inside that pod (portal.py trusts the sidecar's identity headers from
    127.0.0.1 and nowhere else), and calling `/admin/*`, which avoids standing a
    port-forward up per request. Both are the shipped code paths.
    """
    return _run("kubectl", "-n", NS, "exec", _control_plane_pod(), "-c", "control-plane",
                "--", "python3", "-c", script, timeout=180).stdout


_ADMIN_CALL = """
import json, os, httpx
r = httpx.{method}("http://127.0.0.1:8000{path}",
                   headers={{"Authorization": "Bearer " + os.environ["CONTROL_PLANE_ADMIN_TOKEN"]}},
                   timeout=120)
r.raise_for_status()
print(json.dumps(r.json()))
"""


def _admin(path: str, method: str = "get") -> dict:
    return json.loads(_in_control_plane(_ADMIN_CALL.format(method=method, path=path)))


def _portal_spend(user: str) -> dict:
    """`/portal/api/spend` as a signed-in user, through the loopback path oauth2-proxy uses."""
    script = (
        "import json, httpx\n"
        "r = httpx.get('http://127.0.0.1:8000/portal/api/spend',\n"
        f"              headers={{'x-auth-request-preferred-username': {user!r}}}, timeout=120)\n"
        "r.raise_for_status()\n"
        "print(json.dumps(r.json()))\n"
    )
    return json.loads(_in_control_plane(script))


def _collect() -> dict:
    """Force one sample. The SAME `collect_once` the timer runs, not a test-only path."""
    return _admin("/admin/agents/usage/collect", method="post")


def _usage_row() -> dict:
    rows = _admin(f"/admin/agents/usage?username={USER}")["agents"]
    mine = [r for r in rows if r["agent"] == NAME]
    assert mine, f"no usage row for {USER}/{NAME}: {rows}"
    return mine[0]


# ---------------------------------------------------------------- ground truth


def _pod() -> dict:
    items = json.loads(_kubectl(
        "get", "pod", "-l",
        f"agent.enterprise-ai/user={USER},agent.enterprise-ai/name={NAME}", "-o", "json",
    ))["items"]
    assert items, f"no pod for {OBJ}"
    return items[0]


def _pod_start() -> datetime:
    return datetime.fromisoformat(_pod()["status"]["startTime"].replace("Z", "+00:00"))


def _cadvisor_cpu_ground_truth() -> float:
    """The same real counter the meter reads, fetched by a route the meter cannot use.

    `kubectl get --raw /api/v1/nodes/<node>/proxy/metrics/cadvisor` goes through the API
    server on the operator's kubeconfig. The control plane is denied `nodes/proxy` on
    purpose and scrapes the kubelet directly instead, so agreement between the two is
    agreement between two independent readers of one counter — not the code checking
    itself.
    """
    pod = _pod()
    node = pod["spec"]["nodeName"]
    pod_name = pod["metadata"]["name"]
    raw = _run("kubectl", "get", "--raw",
               f"/api/v1/nodes/{node}/proxy/metrics/cadvisor", timeout=180).stdout
    total = 0.0
    for line in raw.splitlines():
        if not line.startswith("container_cpu_usage_seconds_total{"):
            continue
        if f'pod="{pod_name}"' not in line or 'container=""' in line:
            continue
        total += float(line.rsplit("}", 1)[1].split()[0])
    assert total > 0, f"no cAdvisor CPU counter for {pod_name} on {node}"
    return total


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- lifecycle


def _workspace_image() -> str:
    image = _kubectl(
        "get", "pod", "-l", "app.kubernetes.io/component=workspace",
        "-o", 'jsonpath={.items[0].spec.containers[?(@.name=="ttyd")].image}',
    ).strip()
    assert image, "no running workspace pod to read the image from"
    return image


def _teardown() -> None:
    """Remove every object this file creates, INCLUDING the spendable key and its row.

    The same shape -055 and -39d use, for the same reasons: workload first, then the PVC,
    blocking on the PVC so a re-run cannot race a Terminating volume. A deleted Deployment
    does not stop a virtual key spending money, so the key is revoked separately.

    The usage ledger row is deleted too. It is the only row this test writes, and leaving
    a throwaway agent on the operator's usage view forever is the usage-side equivalent of
    leaving the key behind.
    """
    _kubectl("delete", "deployment", OBJ, "--ignore-not-found", "--wait=true",
             check=False, timeout=300)
    _kubectl("delete", "service", OBJ, "--ignore-not-found", check=False)
    _kubectl("delete", "secret", f"{OBJ}-key", "--ignore-not-found", check=False)
    _kubectl("delete", "pvc", OBJ, "--ignore-not-found", "--wait=true",
             check=False, timeout=300)

    _run("kubectl", "-n", NS, "exec", _control_plane_pod(), "-c", "control-plane", "--",
         "python3", "-c",
         "import os, asyncio, httpx, asyncpg\n"
         "async def go():\n"
         "    h = {'Authorization': 'Bearer ' + os.environ['GATEWAY_MASTER_KEY']}\n"
         "    httpx.post('http://gateway:4000/key/delete', headers=h,"
         f"               json={{'key_aliases': ['{ALIAS}']}}, timeout=60)\n"
         "    c = await asyncpg.connect(os.environ['CONTROL_PLANE_DATABASE_URL'])\n"
         f"    await c.execute(\"DELETE FROM virtual_key WHERE key_alias = '{ALIAS}'\")\n"
         "    await c.execute(\"DELETE FROM agent_usage WHERE agent_user = $1 "
         "AND agent_name = $2\", "
         f"'{USER}', '{NAME}')\n"
         "    await c.close()\n"
         "asyncio.run(go())",
         check=False, timeout=180)

    deadline = time.time() + 240
    while time.time() < deadline:
        if not _kubectl("get", "pvc", OBJ, "--ignore-not-found", "-o", "name",
                        check=False).strip():
            return
        time.sleep(3)
    raise AssertionError(f"pvc/{OBJ} still present 240s after delete")


@pytest.fixture(scope="module", autouse=True)
def provisioned_agent():
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


@pytest.fixture(scope="module", autouse=True)
def bill_before(provisioned_agent) -> dict:
    """The inference bill, snapshotted with the agent up and before it has been metered.

    `autouse` so it is taken at module setup rather than lazily when the last test asks
    for it. A "before" snapshot captured after everything has already happened compares
    nothing, and it would have looked exactly as green.
    """
    return {"at": _now(), "spend": _admin("/admin/spend")}


# ---------------------------------------------------------------- the meter


def test_the_collector_can_actually_read_the_cluster():
    """The RBAC landing, proven by what it enables rather than by reading the YAML.

    Before this item the control plane held no Kubernetes authority at all. If the Role,
    the ClusterRole or the ServiceAccount on the Deployment is missing, this is the test
    that says so in one line instead of leaving every number below silently at zero.
    """
    sa = _kubectl("get", "pod", "-l", "app=control-plane",
                  "-o", "jsonpath={.items[0].spec.serviceAccountName}").strip()
    assert sa == "control-plane", (
        f"the control-plane pod runs as ServiceAccount '{sa}'. The resident meter's grants "
        "are bound to 'control-plane' (deploy/k8s/39-control-plane-rbac.yaml); on any "
        "other account it can read no pods and every quantity below is a silent zero."
    )
    result = _collect()
    assert result["enabled"] is True, result
    assert result["agents"] >= 1, (
        f"the collector saw no agent pods while {OBJ} is running: {result}. "
        "Check the Role/RoleBinding in deploy/k8s/39-control-plane-rbac.yaml."
    )
    assert result["compute_measured"] >= 1, (
        f"no agent's compute counter was readable: {result}. The cAdvisor scrape needs the "
        "ClusterRole on nodes/metrics and a route to the kubelet on 10250."
    )


def test_resident_time_is_real_and_bounded_by_the_pods_own_start_time():
    """Resident time is `now - status.startTime`, checked against the pod, not the code.

    Two-sided on purpose. A lower bound alone passes for a meter that credits the pod's
    whole life on every tick; an upper bound alone passes for one that records nothing.
    The recorded total must sit inside the window the POD's own clock allows.
    """
    _collect()
    row = _usage_row()
    elapsed = (_now() - _pod_start()).total_seconds()

    assert row["compute_measured"] is True, row
    assert row["running"] is True, row
    assert row["resident_seconds"] > 0, row
    assert row["resident_seconds"] <= elapsed + 2, (
        f"recorded {row['resident_seconds']}s resident for a pod that has existed "
        f"{elapsed:.1f}s. A meter that can exceed the pod's own age is double-counting."
    )
    # The collector cannot have credited the whole life if it started sampling late, so
    # the floor is loose; what it may not be is nothing at all.
    assert row["resident_seconds"] >= min(elapsed, 5) - 2, (
        f"recorded only {row['resident_seconds']}s for a pod {elapsed:.1f}s old: {row}"
    )
    # The unit conversion is the number a page renders; deriving it twice is how two
    # views come to disagree.
    assert row["resident_hours"] == pytest.approx(row["resident_seconds"] / 3600, rel=1e-3)


def test_resident_time_increases_while_the_agent_runs():
    first = (_collect(), _usage_row())[1]["resident_seconds"]
    time.sleep(GROWTH_WINDOW_S)
    second = (_collect(), _usage_row())[1]["resident_seconds"]
    grew = second - first
    assert grew >= GROWTH_WINDOW_S * 0.8, (
        f"resident time moved {grew:.1f}s across a {GROWTH_WINDOW_S}s window "
        f"({first} -> {second}). A resident meter that does not advance for a running pod "
        "is measuring nothing."
    )
    assert grew <= GROWTH_WINDOW_S + 10, (
        f"resident time moved {grew:.1f}s across a {GROWTH_WINDOW_S}s window "
        f"({first} -> {second}) — more wall clock than elapsed, so an interval is being "
        "counted twice."
    )


def test_compute_is_the_real_cadvisor_counter_for_this_pod():
    """Agreement with an independent reader of the same counter, and the units.

    The tolerance is one-sided and asymmetric for a stated reason: the ledger's total is
    accumulated from samples taken slightly EARLIER than this test's own scrape, and the
    counter only goes up, so the ledger may lag the ground truth but must never lead it.
    Leading it would mean the meter is inventing CPU.
    """
    _collect()
    row = _usage_row()
    truth = _cadvisor_cpu_ground_truth()

    # The comparison is total-against-total, so it is only meaningful while the agent is
    # on its FIRST pod: the ledger accumulates across incarnations and cAdvisor's counter
    # restarts with each one. Asserted rather than assumed, because a test that silently
    # stopped comparing anything after a restart is worse than one that fails.
    assert row["resident_seconds"] <= (_now() - _pod_start()).total_seconds() + 2, (
        "this agent has been through more than one pod, so the ledger's accumulated CPU "
        "is no longer comparable to a single pod's counter — run this before the freeze "
        "test restarts it"
    )
    assert row["compute_source"] == "kubelet/cadvisor", row
    assert row["cpu_core_seconds"] > 0, row
    assert row["cpu_core_seconds"] <= truth + 0.5, (
        f"the ledger holds {row['cpu_core_seconds']} CPU-core-seconds but cAdvisor's own "
        f"counter for this pod reads {truth}. The meter cannot exceed the counter it "
        "integrates."
    )
    assert row["cpu_core_seconds"] >= truth * 0.5, (
        f"the ledger holds {row['cpu_core_seconds']} CPU-core-seconds against a real "
        f"counter of {truth}. More than half the pod's CPU is unaccounted for."
    )
    assert row["cpu_core_hours"] == pytest.approx(row["cpu_core_seconds"] / 3600, rel=1e-3)
    assert row["memory_peak_bytes"] > 0, row
    assert row["memory_peak_mb"] == pytest.approx(row["memory_peak_bytes"] / 1e6, rel=1e-3)


def test_usage_is_attributed_to_the_right_user_and_agent():
    """The attribution key is the POD's labels, and it lands on the right owner.

    Read as the two halves that can go wrong independently: the row names the right pair,
    and the per-user view for somebody else does not contain it.
    """
    _collect()
    row = _usage_row()
    labels = _pod()["metadata"]["labels"]
    assert row["user"] == labels["agent.enterprise-ai/user"] == USER
    assert row["agent"] == labels["agent.enterprise-ai/name"] == NAME
    assert row["surface"] == SURFACE, (
        f"the usage row's surface is {row['surface']!r}; it must be Contract 1's "
        f"per-instance surface {SURFACE!r} so it lines up with the inference row for the "
        "same agent without anybody parsing anything."
    )
    assert row["model_source"] == labels["agent.enterprise-ai/model-source"] == "integrated"

    others = _admin("/admin/agents/usage?username=claire")["agents"]
    assert not [r for r in others if r["agent"] == NAME], (
        f"{USER}'s agent appeared under claire's usage: {others}"
    )


def test_the_usage_ledger_carries_no_money_anywhere():
    """Baron's ruling, made mechanical.

    Owned hardware is sunk cost; inference already tracks a real Forge cost and compute
    does not. So this payload is quantities only. The check is on the SERIALISED response,
    because that is what a page renders and what an integrator would find — a dollar
    figure computed in the renderer instead of here would still be a dollar figure the
    ruling forbids.
    """
    payload = _admin("/admin/agents/usage")
    assert payload["units"] == {
        "resident": "hours",
        "compute": "cpu-core-hours",
        "memory": "megabytes (peak working set)",
    }, payload["units"]

    text = json.dumps(payload).lower()
    for forbidden in ("usd", "dollar", "$", "rate_per", "price", "cost"):
        assert forbidden not in text, (
            f"the usage payload contains {forbidden!r}: {payload}. Baron ruled that "
            "resident time and compute are metered as USAGE, not priced. If commodity "
            "cloud compute is ever added, cost-wiring is a separate item."
        )
    row = _usage_row()
    assert "spend" not in row and "cost" not in row, row


# ---------------------------------------------------------------- the freeze


def test_a_stopped_agent_freezes_its_usage_and_stops_reporting_as_running():
    """Contract 2's whole claim: `stopped` is `replicas: 0`, so the meter has nothing to read.

    Not "billed at a stopped rate" — there is no pod, no `status.startTime` advancing and
    no cAdvisor counter incrementing, so the quantities are FINAL. Proven by stopping a
    real agent and sampling the real ledger twice across a window longer than the
    collector's own interval, so a tick genuinely happens inside it.

    Then started again, because the sharper failure is the one that only shows up on
    resume: a meter that re-anchors on `startTime` alone would credit the entire stopped
    interval the moment the agent comes back, and the frozen reading above would have
    looked perfect right up until it did.
    """
    _collect()
    frozen_at = _usage_row()

    _kubectl("scale", f"deployment/{OBJ}", "--replicas=0")
    _run("kubectl", "-n", NS, "wait", "--for=delete", "pod", "-l",
         f"agent.enterprise-ai/user={USER},agent.enterprise-ai/name={NAME}",
         "--timeout=300s", check=False, timeout=330)
    assert not _kubectl(
        "get", "pod", "-l", f"agent.enterprise-ai/name={NAME}", "-o", "name",
    ).strip(), "the pod is still there after scaling to zero"

    _collect()
    first = _usage_row()
    assert first["running"] is False, first
    assert first["pod_phase"] == "Absent", (
        f"a stopped agent still reports pod_phase {first['pod_phase']!r}. Its numbers had "
        "quietly stopped moving while the view went on saying it was running, which is "
        "the worst of both readings."
    )

    time.sleep(FREEZE_WINDOW_S)
    _collect()
    second = _usage_row()

    for field in ("resident_seconds", "resident_hours", "cpu_core_seconds",
                  "cpu_core_hours", "memory_peak_bytes"):
        assert second[field] == first[field], (
            f"{field} moved from {first[field]} to {second[field]} across {FREEZE_WINDOW_S}s "
            f"with the agent STOPPED (replicas: 0, no pod). Contract 2's zero-cost claim "
            "depends on there being nothing to sample; a number that grows without a pod "
            "is being computed rather than measured."
        )
    assert first["resident_seconds"] >= frozen_at["resident_seconds"], (
        "the total fell when the agent stopped; these counters only ever go up"
    )

    # --- and the resume, which is where a wrong anchor shows itself
    stopped_since = _now()
    _kubectl("scale", f"deployment/{OBJ}", "--replicas=1")
    _kubectl("rollout", "status", f"deployment/{OBJ}", "--timeout=600s", timeout=650)
    stopped_seconds = (_pod_start() - stopped_since).total_seconds() + FREEZE_WINDOW_S

    _collect()
    resumed = _usage_row()
    running_again_for = (_now() - _pod_start()).total_seconds()
    credited = resumed["resident_seconds"] - second["resident_seconds"]
    assert credited <= running_again_for + 2, (
        f"restarting the agent credited {credited:.1f}s of resident time, but the new pod "
        f"has only existed for {running_again_for:.1f}s — the ~{stopped_seconds:.0f}s it "
        "spent stopped was billed as if it were up. The meter must anchor on the last "
        "observation of the SAME pod, not on startTime alone."
    )
    assert credited > 0, (
        f"the agent was restarted and accrued nothing: {second} -> {resumed}"
    )
    assert resumed["cpu_core_seconds"] >= second["cpu_core_seconds"], (
        "CPU went backwards across a pod replacement: cAdvisor's counter restarts with "
        "the container, so a new incarnation contributes its whole counter and never a "
        "negative delta"
    )


# ---------------------------------------------------------------- beside the bill


def test_the_portal_shows_usage_next_to_that_agents_inference_spend():
    """The two dimensions, in one payload, joined on Contract 1's per-instance surface.

    The point of the item: a user opening their own page sees what the agent SPENT on
    inference and what it USED in hours and core-hours, next to each other, without either
    number having been folded into the other.
    """
    _collect()
    payload = _portal_spend(USER)

    # Everything the endpoint returned before this landed is still there, unchanged in
    # meaning: `total` totals INFERENCE spend and nothing else.
    for key in ("username", "since", "by_surface", "total"):
        assert key in payload, payload
    assert set(payload["total"]) == {"requests", "spend", "prompt_tokens", "completion_tokens"}
    assert "agents_usage_error" not in payload, payload
    assert "by_agent" in payload, payload

    mine = [a for a in payload["by_agent"] if a["agent"] == NAME]
    assert mine, f"no by_agent entry for {NAME}: {payload['by_agent']}"
    entry = mine[0]

    assert entry["surface"] == SURFACE
    assert entry["usage"]["resident_hours"] > 0, entry
    assert entry["usage"]["cpu_core_hours"] > 0, entry
    assert entry["usage"]["memory_peak_mb"] > 0, entry
    assert entry["usage"]["compute_source"] == "kubelet/cadvisor", entry
    # Integrated: its inference IS on our ledger, so the spend figure is a real one (zero
    # here, because this agent was never asked to think) rather than "off-ledger".
    assert entry["inference"]["on_ledger"] is True, entry
    assert set(entry["inference"]) == {
        "on_ledger", "requests", "spend", "prompt_tokens", "completion_tokens"
    }, entry
    assert "spend" not in entry["usage"] and "cost" not in entry["usage"], entry

    # A different user's page must not carry it.
    assert not [a for a in _portal_spend("claire")["by_agent"] if a["agent"] == NAME]


def test_the_inference_bill_is_unperturbed(bill_before):
    """`/admin/spend` must not move because a usage meter landed.

    HOW THIS IS EXACT ON A CLUSTER THAT IS SERVING PEOPLE. Comparing two all-time
    snapshots would fail whenever somebody used chat mid-run, which is a flaky test and
    therefore a broken one. So the assertion is an IDENTITY that holds under any amount of
    concurrent traffic:

        (all-time now)  -  (all-time before)  ==  (since the "before" instant)

    per (user, surface). It is exactly the arithmetic the ledger owes, and it breaks the
    moment attribution shifts, a row falls out, or the query starts answering a different
    question — which are the three ways a change like this one perturbs a bill.

    Plus the sharp deterministic half: this agent performed no inference at all, so its
    surface must not appear on the bill under any total, ever.
    """
    before = bill_before["spend"]
    # `Z`, not `+00:00`. A `+` in a query string decodes to a space, so the offset form
    # reaches Postgres as `2026-08-10T03:28:17 00:00` and the cast fails — a 500 that
    # looks like a broken bill and is really a URL.
    since = bill_before["at"].strftime("%Y-%m-%dT%H:%M:%SZ")
    after = _admin("/admin/spend")
    delta = _admin(f"/admin/spend?since={since}")

    def keyed(payload):
        return {(r["username"], r["surface"]): r for r in payload["by_user_and_surface"]}

    b, a, d = keyed(before), keyed(after), keyed(delta)

    assert set(b) <= set(a), (
        f"rows vanished from the bill: {sorted(set(b) - set(a))}. Money that was "
        "definitely spent must never leave it."
    )
    for key in a:
        moved = a[key]["spend"] - b.get(key, {}).get("spend", 0.0)
        claimed = d.get(key, {}).get("spend", 0.0)
        assert moved == pytest.approx(claimed, abs=1e-9), (
            f"{key}: the all-time bill moved by {moved} but the bill since "
            f"{since} reports {claimed}. The two renderings of one ledger disagree, which "
            "is what a perturbed spend query looks like."
        )
        requests_moved = a[key]["requests"] - b.get(key, {}).get("requests", 0)
        assert requests_moved == d.get(key, {}).get("requests", 0), key

    assert after["totals"]["requests"] >= before["totals"]["requests"]
    assert after["totals"]["spend"] >= before["totals"]["spend"]

    on_the_bill = [k for k in a if k[1] == SURFACE]
    assert not on_the_bill, (
        f"{SURFACE} appears on the inference bill ({on_the_bill}) although this agent "
        "never made a single model call. The resident meter has leaked into the spend "
        "ledger, which is precisely what a separate table is for."
    )
    assert "agent_usage" not in json.dumps(after), (
        "the inference bill is carrying resident-usage fields; the two dimensions are "
        "composed in the endpoint layer and never folded into one another"
    )
