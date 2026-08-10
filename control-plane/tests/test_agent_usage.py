"""The resident meter's arithmetic, and the promise that adding it moved nothing.

Contract 3(b) of docs/design/records/agents-surface.md, with Baron's ruling on top of it:
resident time and compute are metered as USAGE — hours, CPU-core-hours, megabytes — and
never priced. Owned hardware is sunk cost and there is no dollar figure to record.

WHERE THE EXPECTED VALUES IN THIS FILE COME FROM

Not from the code under test. Every number below was recorded from the live k3s cluster
while `agent-baron-usage914` was actually running, and it is reproduced here verbatim:

  * `CADVISOR_SAMPLE` is a real kubelet `/metrics/cadvisor` excerpt, copied unmodified,
    including its cgroup `id=` labels and its scientific-notation memory value.
  * `POD` is the real pod object's labels and status.
  * `TRANSCRIPT` is the sequence of observations the collector actually made, and the
    totals asserted against them are what `/admin/agents/usage` actually returned at each
    step on the cluster — the real ledger, read back through the real endpoint.

The half that cannot be faked — that the meter tracks a real pod, freezes when it is
stopped and does not credit the stopped interval on resume — is
tests-live/test_agent_usage.py against the live cluster. What is here is the arithmetic
those measurements pinned down, so a regression in the fold is caught without a cluster.
"""

import subprocess
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
REPO = ROOT.parent
sys.path.insert(0, str(ROOT))

# The driver, not the rule — the same trade the sibling suites make. `app.db` binds
# asyncpg at import and `app.agent_usage` imports it for the connection pool; the test
# venv deliberately carries no database driver (bundle/bin/run-tests.sh: the venv exists
# to prove behaviour, not to host a database). Nothing below opens a connection, and the
# modules actually under test — `app.agent_usage` and `app.portal` — are imported for real.
if "asyncpg" not in sys.modules:
    _pg = types.ModuleType("asyncpg")
    _pg.Pool = object

    async def _create_pool(*a, **kw):  # pragma: no cover - never reached
        raise RuntimeError("no database in this suite")

    _pg.create_pool = _create_pool
    sys.modules["asyncpg"] = _pg

from app import agent_usage  # noqa: E402

UTC = timezone.utc


def _t(text: str) -> datetime:
    return datetime.fromisoformat(text)


# ---------------------------------------------------------------- real recorded data

# Copied byte-for-byte from `kubectl get --raw
# /api/v1/nodes/k3s-worker/proxy/metrics/cadvisor` on 2026-08-10, filtered to the one
# agent pod. Three CPU series and three memory series: the POD cgroup (container=""), the
# pause container (container="", with an image), and the agent's own container.
CADVISOR_SAMPLE = """\
container_cpu_usage_seconds_total{container="",cpu="total",id="/kubepods.slice/kubepods-burstable.slice/kubepods-burstable-pod7c57de98_0c20_4854_bfd1_e242e1b0940b.slice",image="",name="",namespace="enterprise-ai",pod="agent-baron-usage914-db6576484-k9vkt"} 4.656145 1786331963155
container_cpu_usage_seconds_total{container="",cpu="total",id="/kubepods.slice/kubepods-burstable.slice/kubepods-burstable-pod7c57de98_0c20_4854_bfd1_e242e1b0940b.slice/cri-containerd-b1fb07440584c3c72e3b90be85f4c7eaf2821507889036443f766eecd1fe9297.scope",image="docker.io/rancher/mirrored-pause:3.6",name="b1fb07440584c3c72e3b90be85f4c7eaf2821507889036443f766eecd1fe9297",namespace="enterprise-ai",pod="agent-baron-usage914-db6576484-k9vkt"} 0.094545 1786331958344
container_cpu_usage_seconds_total{container="agent",cpu="total",id="/kubepods.slice/kubepods-burstable.slice/kubepods-burstable-pod7c57de98_0c20_4854_bfd1_e242e1b0940b.slice/cri-containerd-145de86c73e55d2696305710df52707a579a0f73bc5f2ba10c0d33233a1c5b47.scope",image="192.168.2.43:30500/enterprise-ai-workspace:edits-c4a41a2",name="145de86c73e55d2696305710df52707a579a0f73bc5f2ba10c0d33233a1c5b47",namespace="enterprise-ai",pod="agent-baron-usage914-db6576484-k9vkt"} 4.560939 1786331962719
container_memory_working_set_bytes{container="",id="/kubepods.slice/kubepods-burstable.slice/kubepods-burstable-pod7c57de98_0c20_4854_bfd1_e242e1b0940b.slice",image="",name="",namespace="enterprise-ai",pod="agent-baron-usage914-db6576484-k9vkt"} 3.04140288e+08 1786331963155
container_memory_working_set_bytes{container="",id="/kubepods.slice/kubepods-burstable.slice/kubepods-burstable-pod7c57de98_0c20_4854_bfd1_e242e1b0940b.slice/cri-containerd-b1fb07440584c3c72e3b90be85f4c7eaf2821507889036443f766eecd1fe9297.scope",image="docker.io/rancher/mirrored-pause:3.6",name="b1fb07440584c3c72e3b90be85f4c7eaf2821507889036443f766eecd1fe9297",namespace="enterprise-ai",pod="agent-baron-usage914-db6576484-k9vkt"} 212992 1786331958344
container_memory_working_set_bytes{container="agent",id="/kubepods.slice/kubepods-burstable.slice/kubepods-burstable-pod7c57de98_0c20_4854_bfd1_e242e1b0940b.slice/cri-containerd-145de86c73e55d2696305710df52707a579a0f73bc5f2ba10c0d33233a1c5b47.scope",image="192.168.2.43:30500/enterprise-ai-workspace:edits-c4a41a2",name="145de86c73e55d2696305710df52707a579a0f73bc5f2ba10c0d33233a1c5b47",namespace="enterprise-ai",pod="agent-baron-usage914-db6576484-k9vkt"} 3.03890432e+08 1786331962719
"""

POD_NAME = "agent-baron-usage914-db6576484-k9vkt"

# The real pod object, reduced to the fields the meter reads.
POD = {
    "metadata": {
        "name": POD_NAME,
        "uid": "7c57de98-0c20-4854-bfd1-e242e1b0940b",
        "labels": {
            "app.kubernetes.io/component": "agent",
            "agent.enterprise-ai/user": "baron",
            "agent.enterprise-ai/name": "usage914",
            "agent.enterprise-ai/model-source": "integrated",
        },
    },
    "status": {
        "phase": "Running",
        "startTime": "2026-08-10T03:19:05Z",
        "hostIP": "192.168.2.44",
    },
}

FIRST_UID = "cb333a04-01de-4654-b510-9768bf86e621"
SECOND_UID = "7c57de98-0c20-4854-bfd1-e242e1b0940b"


def _obs(uid, start, phase="Running", cpu=None, mem=None, source=None):
    return agent_usage.Observation(
        user="baron", agent="usage914", pod_uid=uid, pod_name=POD_NAME,
        phase=phase, started_at=_t(start), node_ip="192.168.2.44",
        model_source="integrated", cpu_core_seconds=cpu, memory_bytes=mem,
        compute_source=source,
    )


# The collector's real transcript on 2026-08-10, and beside each step the totals
# `/admin/agents/usage` actually returned after it. Sample 3 is the agent STOPPED
# (`replicas: 0`): there is no pod, so the collector observes nothing, which is why the
# entry is `None` and why the expected totals are unchanged.
FIRST_START = "2026-08-10T03:15:51+00:00"
SECOND_START = "2026-08-10T03:19:05+00:00"

TRANSCRIPT = [
    # (when the sample was taken, the observation, resident_seconds, cpu_core_seconds)
    ("2026-08-10T03:16:01.681091+00:00",
     _obs(FIRST_UID, FIRST_START, cpu=0.056, mem=1695744, source="kubelet/cadvisor"),
     10.681, 0.056),
    ("2026-08-10T03:16:45.376932+00:00",
     _obs(FIRST_UID, FIRST_START, cpu=3.557, mem=271331328, source="kubelet/cadvisor"),
     54.377, 3.557),
    ("2026-08-10T03:18:10+00:00", None, 54.377, 3.557),
    ("2026-08-10T03:19:19.938068+00:00",
     _obs(SECOND_UID, SECOND_START, cpu=0.205, mem=271331328, source="kubelet/cadvisor"),
     69.315, 3.762),
]


# ---------------------------------------------------------------- reading the cluster


def test_the_parser_reads_the_real_cadvisor_sample():
    """The two quantities, off a real scrape, with the two decoy series excluded.

    The decoys are the point. Summing every series in the sample would give
    4.656145 + 0.094545 + 4.560939 CPU-seconds for one pod — roughly double the truth,
    because the pod cgroup already contains both containers. The expected values here are
    the numbers a person can read off `CADVISOR_SAMPLE` on the `container="agent"` lines.
    """
    parsed = agent_usage.parse_cadvisor(CADVISOR_SAMPLE, "enterprise-ai")
    assert set(parsed) == {POD_NAME}, parsed
    assert parsed[POD_NAME]["cpu_core_seconds"] == pytest.approx(4.560939)
    assert parsed[POD_NAME]["memory_bytes"] == 303890432

    everything = 4.656145 + 0.094545 + 4.560939
    assert parsed[POD_NAME]["cpu_core_seconds"] != pytest.approx(everything), (
        "the pod cgroup and the pause container were counted as well as the agent's own "
        "container, which double-counts every agent's CPU"
    )


def test_another_namespace_is_never_attributed_here():
    """One kubelet serves every namespace on its node, including a tenant's own pods."""
    assert agent_usage.parse_cadvisor(CADVISOR_SAMPLE, "somebody-else") == {}


def test_the_label_parser_survives_a_real_cgroup_path():
    """`id=` is a cgroup path full of `/`, `.`, `-` and `_`.

    A parser that mis-splits one line does not error — it attributes that line's CPU to
    whatever it thinks the pod label was, which is a wrong number on somebody's usage row
    with nothing on screen to say so.
    """
    line = CADVISOR_SAMPLE.splitlines()[2]
    labels = agent_usage._parse_labels(line.partition("{")[2].partition("}")[0])
    assert labels["pod"] == POD_NAME
    assert labels["container"] == "agent"
    assert labels["namespace"] == "enterprise-ai"
    assert labels["cpu"] == "total"
    assert labels["id"].startswith("/kubepods.slice/")
    assert labels["id"].endswith(".scope")


def test_the_observation_comes_from_the_pods_own_labels():
    """Contract 3's attribution key: the pod's labels, not the virtual key.

    Compute is consumed by the pod, so a BYO agent that produces no gateway ledger row at
    all still has an owner here.
    """
    obs = agent_usage.observation_from_pod(POD)
    assert (obs.user, obs.agent) == ("baron", "usage914")
    assert obs.pod_uid == SECOND_UID
    assert obs.started_at == _t(SECOND_START)
    assert obs.node_ip == "192.168.2.44"
    assert obs.model_source == "integrated"
    assert obs.running is True
    # Never read yet: absent is not zero.
    assert obs.cpu_core_seconds is None and obs.compute_source is None


def test_a_pod_missing_half_the_attribution_key_is_skipped():
    """A usage row nobody owns is worse than a missing one — it looks like somebody's."""
    for drop in ("agent.enterprise-ai/user", "agent.enterprise-ai/name"):
        pod = {"metadata": {**POD["metadata"],
                            "labels": {k: v for k, v in POD["metadata"]["labels"].items()
                                       if k != drop}},
               "status": POD["status"]}
        assert agent_usage.observation_from_pod(pod) is None


# ---------------------------------------------------------------- the meter


def test_the_meter_reproduces_the_live_ledger():
    """Replay the real transcript and land on the totals the real ledger held.

    This is the whole meter. The expected values are what `/admin/agents/usage` returned
    on the cluster at each step, so a fold that drifts from them is a fold that would have
    produced different hours for a real agent.
    """
    state = None
    for when, obs, resident, cpu in TRANSCRIPT:
        if obs is not None:
            state = agent_usage.accrue(state, obs, _t(when))
        assert round(state["resident_seconds"], 3) == resident, (
            f"at {when}: resident {state['resident_seconds']} != the {resident}s the "
            "cluster's ledger actually held"
        )
        assert round(state["cpu_core_seconds"], 3) == cpu, (
            f"at {when}: cpu {state['cpu_core_seconds']} != the {cpu} core-seconds the "
            "cluster's ledger actually held"
        )
    assert state["memory_peak_bytes"] == 271331328


def test_a_stopped_agent_accrues_nothing_because_there_is_nothing_to_observe():
    """Contract 2's zero, stated the way the mechanism states it.

    Sample 3 of the transcript is the agent at `replicas: 0`. There is no pod, so there is
    no observation, so `accrue` is never called and the totals are final. The freeze is not
    a rule the meter applies — it is the absence of an input, which is why it cannot be got
    wrong by a later change to the arithmetic.
    """
    state = None
    for when, obs, _r, _c in TRANSCRIPT[:2]:
        state = agent_usage.accrue(state, obs, _t(when))

    # 85 seconds of wall clock with the agent at replicas: 0. The recorded transcript
    # carries NO observation for that step, and the totals the cluster's ledger held
    # after it are the same ones it held before it.
    when, obs, resident, cpu = TRANSCRIPT[2]
    assert obs is None, "the stopped step must carry no observation at all"
    assert (resident, cpu) == (TRANSCRIPT[1][2], TRANSCRIPT[1][3])
    assert (round(state["resident_seconds"], 3), round(state["cpu_core_seconds"], 3)) == (
        resident, cpu
    )


def test_a_non_running_pod_accrues_no_resident_time():
    """`created` is a pod that has not started yet. Accrual begins at Running, per Contract 2."""
    state = agent_usage.accrue(
        None, _obs(SECOND_UID, SECOND_START, phase="Pending"), _t("2026-08-10T03:20:00+00:00")
    )
    assert state["resident_seconds"] == 0.0
    assert state["last_pod_phase"] == "Pending"


def test_resuming_a_stopped_agent_does_not_bill_the_time_it_was_stopped():
    """The failure a `startTime`-only anchor produces, and the one this file exists to pin.

    The real agent was stopped at 03:16:45 and started again at 03:19:05 — 140 seconds
    down. At 03:19:19.938 a meter anchored on `startTime` alone would still be right
    (14.9s), because the new pod's startTime is after the outage. The bug shows up on a
    pod that was NEVER stopped and simply not sampled for a while, and on the second tick
    after a resume. So both are checked here: the fourth transcript step credits only the
    new pod's own age, and a further tick credits only the interval since the last sample.
    """
    state = None
    for when, obs, _r, _c in TRANSCRIPT:
        if obs is not None:
            state = agent_usage.accrue(state, obs, _t(when))

    credited_on_resume = state["resident_seconds"] - 54.376932
    assert credited_on_resume == pytest.approx(14.938068, abs=1e-6), (
        f"the resume credited {credited_on_resume}s; the new pod had existed for 14.94s"
    )

    later = agent_usage.accrue(
        state,
        _obs(SECOND_UID, SECOND_START, cpu=4.560939, mem=303890432, source="kubelet/cadvisor"),
        _t("2026-08-10T03:19:29.938068+00:00"),
    )
    assert later["resident_seconds"] - state["resident_seconds"] == pytest.approx(10.0)
    # The CPU counter for the SAME container: only the delta, never the whole counter again.
    assert later["cpu_core_seconds"] - state["cpu_core_seconds"] == pytest.approx(
        4.560939 - 0.205
    )
    assert later["memory_peak_bytes"] == 303890432


def test_a_missed_compute_sample_costs_nothing():
    """Why the design record chose a counter over metrics-server's gauge.

    The middle sample fails to read cAdvisor. The counter is not reset and not guessed, so
    the next successful read recovers the entire interval — the total is identical to the
    run where no sample was missed. With a gauge that interval would be gone for good, and
    a quantity nobody can reconcile is worse than no quantity at all.
    """
    def run(with_gap: bool):
        state = None
        state = agent_usage.accrue(
            state, _obs(FIRST_UID, FIRST_START, cpu=0.056, source="kubelet/cadvisor"),
            _t("2026-08-10T03:16:01.681091+00:00"))
        if with_gap:
            state = agent_usage.accrue(
                state, _obs(FIRST_UID, FIRST_START), _t("2026-08-10T03:16:20+00:00"))
        state = agent_usage.accrue(
            state, _obs(FIRST_UID, FIRST_START, cpu=3.557, source="kubelet/cadvisor"),
            _t("2026-08-10T03:16:45.376932+00:00"))
        return state

    assert run(True)["cpu_core_seconds"] == pytest.approx(run(False)["cpu_core_seconds"])
    assert run(True)["cpu_core_seconds"] == pytest.approx(3.557)
    # And resident time is unaffected by a compute failure: it comes from the pod object.
    assert run(True)["resident_seconds"] == pytest.approx(run(False)["resident_seconds"])


def test_a_counter_that_went_backwards_is_never_a_negative_delta():
    """An in-place container restart resets cAdvisor's counter. A meter that can go down is not one."""
    state = agent_usage.accrue(
        None, _obs(FIRST_UID, FIRST_START, cpu=900.0, source="kubelet/cadvisor"),
        _t("2026-08-10T03:16:01+00:00"))
    after = agent_usage.accrue(
        state, _obs(FIRST_UID, FIRST_START, cpu=1.5, source="kubelet/cadvisor"),
        _t("2026-08-10T03:16:31+00:00"))
    assert after["cpu_core_seconds"] == pytest.approx(901.5)
    assert after["cpu_core_seconds"] >= state["cpu_core_seconds"]


def test_a_compute_source_of_none_is_not_the_same_as_zero_compute():
    """Provenance travels with the number, so "not measured" cannot read as "idle"."""
    unmeasured = agent_usage.as_usage_row(
        agent_usage.accrue(None, _obs(FIRST_UID, FIRST_START),
                           _t("2026-08-10T03:16:01+00:00")))
    assert unmeasured["cpu_core_seconds"] == 0.0
    assert unmeasured["compute_source"] is None
    assert unmeasured["compute_measured"] is False

    measured = agent_usage.as_usage_row(
        agent_usage.accrue(None, _obs(FIRST_UID, FIRST_START, cpu=0.0, mem=0,
                                      source="kubelet/cadvisor"),
                           _t("2026-08-10T03:16:01+00:00")))
    assert measured["cpu_core_seconds"] == 0.0
    assert measured["compute_measured"] is True


# ---------------------------------------------------------------- usage, not cost


def test_the_rendered_usage_row_carries_quantities_and_no_money():
    """Baron's ruling as a shape check on what every reader receives.

    Owned hardware is sunk cost; inference already tracks a real Forge cost and compute
    does not. If commodity cloud compute is ever added, cost-wiring is a FUTURE item — the
    quantities below are the seam it would multiply.
    """
    row = agent_usage.as_usage_row(
        agent_usage.accrue(None, _obs(SECOND_UID, SECOND_START, cpu=3600.0,
                                      mem=2_000_000_000, source="kubelet/cadvisor"),
                           _t("2026-08-10T04:19:05+00:00")))

    assert row["resident_hours"] == 1.0
    assert row["cpu_core_hours"] == 1.0
    assert row["memory_peak_mb"] == 2000.0
    assert row["surface"] == "agents/usage914"

    forbidden = ("cost", "price", "rate", "spend", "usd", "dollar", "amount", "charge")
    for key in row:
        assert not any(word in key.lower() for word in forbidden), (
            f"the usage row carries a money-shaped field {key!r}. Resident time and "
            "compute are metered as USAGE, not priced."
        )


def test_the_meter_never_reads_the_inference_ledger():
    """Two dimensions, two sources. They are composed in the endpoint layer and nowhere else.

    `metering.py` reads the GATEWAY's database. If the usage collector imported it, the
    obvious next step would be a join in SQL — and a query owning both dimensions is
    exactly how a change to the usage meter comes to move the operator's bill.
    """
    import ast

    tree = ast.parse((ROOT / "app" / "agent_usage.py").read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.update(a.name for a in node.names)
            if node.module:
                imported.add(node.module.split(".")[0])
    assert "metering" not in imported, sorted(imported)
    assert "export" not in imported, sorted(imported)

    # And it never names the gateway's tables. The prose in this module talks ABOUT the
    # inference ledger at length, so the check is on imports and on SQL identifiers, not
    # on the word.
    source = (ROOT / "app" / "agent_usage.py").read_text()
    assert "LiteLLM_SpendLogs" not in source
    assert "GATEWAY_DATABASE_URL" not in source


def test_the_inference_query_is_byte_identical_to_the_pre_agents_baseline():
    """"The operator's bill did not move" — a measurement, not a promise.

    The same mechanism tests/test_agents_code_untouched.py applies to the Code surface,
    pointed at the one file that owns the spend queries. The baseline is the commit at
    which the Agents epic began, spelled out here rather than imported because
    control-plane/tests/ is a separate root — if the two ever disagree, both are still
    pinned to a commit and neither can drift silently.

    THIS VALUE MUST NOT BE "REFRESHED" TO MAKE A FAILURE GO AWAY. A red here means the
    inference ledger's attribution or its SQL changed inside an item that promised to be
    additive; the fix is to revert that hunk, not to move the baseline.
    """
    baseline = "5942a5ccc3acea87b048a02d904cf33407718c6d"
    present = subprocess.run(["git", "cat-file", "-e", f"{baseline}^{{commit}}"],
                             cwd=REPO, capture_output=True, text=True, timeout=60)
    assert present.returncode == 0, (
        f"the pinned Agents baseline {baseline} is not in this checkout "
        f"({present.stderr.strip()}). Fetch full history — a shallow clone cannot prove "
        "the bill is unchanged and must not be allowed to look like it did."
    )
    diff = subprocess.run(
        ["git", "diff", "--exit-code", baseline, "--", "control-plane/app/metering.py"],
        cwd=REPO, capture_output=True, text=True, timeout=120,
    )
    assert diff.returncode == 0, (
        "control-plane/app/metering.py changed since the Agents epic began. It owns "
        "`spend_by_user_and_surface`, `ledger_attribution_sql` and `totals` — the one "
        "bill. The resident meter is a separate table read by a separate module and "
        "composed in the endpoint layer; nothing in it needs this file to change:\n\n"
        f"{diff.stdout}{diff.stderr}"
    )


def test_the_check_bites_when_the_bill_is_touched():
    """Fault injection, through the real path: a real byte, the real file, the real git diff.

    An invariant that passes is worth nothing unless it can come back red, and the failure
    mode worth guarding is a typo'd path watching nothing at all.
    """
    victim = REPO / "control-plane" / "app" / "metering.py"
    original = victim.read_bytes()
    baseline = "5942a5ccc3acea87b048a02d904cf33407718c6d"
    try:
        victim.write_bytes(original + b"\n# fault injection: test_agent_usage.py\n")
        proc = subprocess.run(
            ["git", "diff", "--exit-code", baseline, "--", "control-plane/app/metering.py"],
            cwd=REPO, capture_output=True, text=True, timeout=120,
        )
        assert proc.returncode != 0, (
            "the bill's own module was edited on disk and the check still passed — it is "
            "watching nothing"
        )
        assert "metering.py" in proc.stdout
    finally:
        victim.write_bytes(original)


# ---------------------------------------------------------------- beside the bill


def _spend_rows():
    """One real-shaped ledger answer: two base surfaces and one agent instance."""
    return [
        {"username": "baron", "surface": "chat", "requests": 3, "spend": 0.5,
         "prompt_tokens": 100, "completion_tokens": 20},
        {"username": "baron", "surface": "agents/usage914", "requests": 4, "spend": 0.25,
         "prompt_tokens": 40, "completion_tokens": 32},
        {"username": "claire", "surface": "ide", "requests": 9, "spend": 9.0,
         "prompt_tokens": 1, "completion_tokens": 1},
    ]


def test_by_agent_is_added_beside_the_spend_rows_and_changes_none_of_them():
    """Additivity, measured: the same call with and without a usage ledger.

    Every pre-existing key must be identical. `total` is INFERENCE spend and goes on
    meaning exactly that — folding resident usage into it is the failure this shape
    forecloses, and it is the one an "agents cost this much" field would quietly be.
    """
    import asyncio

    from app import metering, portal

    usage = [agent_usage.as_usage_row(
        agent_usage.accrue(None, _obs(SECOND_UID, SECOND_START, cpu=7200.0,
                                      mem=500_000_000, source="kubelet/cadvisor"),
                           _t("2026-08-10T05:19:05+00:00")))]

    async def _spend(since=None):
        return _spend_rows()

    async def go(usage_rows):
        async def _usage(user=None):
            return [r for r in usage_rows if user is None or r["user"] == user]

        original_spend = metering.spend_by_user_and_surface
        original_usage = agent_usage.usage_by_agent
        metering.spend_by_user_and_surface = _spend
        agent_usage.usage_by_agent = _usage
        try:
            return await portal.my_spend(user="baron")
        finally:
            metering.spend_by_user_and_surface = original_spend
            agent_usage.usage_by_agent = original_usage

    without = asyncio.run(go([]))
    with_usage = asyncio.run(go(usage))

    for key in ("username", "since", "by_surface", "total"):
        assert with_usage[key] == without[key], (
            f"{key} changed when the usage ledger became non-empty; the resident meter is "
            "supposed to be added BESIDE inference spend, never folded into it"
        )
    assert without["by_agent"] == []
    assert with_usage["total"]["spend"] == 0.75

    entry = with_usage["by_agent"][0]
    assert entry["agent"] == "usage914"
    assert entry["usage"]["resident_hours"] == 2.0
    assert entry["usage"]["cpu_core_hours"] == 2.0
    # The join key is Contract 1's per-instance surface, so the agent's own inference row
    # lands next to its own usage without anybody parsing an alias.
    assert entry["inference"]["spend"] == 0.25
    assert entry["inference"]["requests"] == 4
    assert entry["inference"]["on_ledger"] is True


def test_a_byo_agent_shows_off_ledger_by_design_and_never_a_silent_zero():
    """Contract 4's visibility rule, on the surface that renders it.

    A BYO agent's inference never traverses this layer, so it has no ledger row — that is
    permitted precisely because it is DECLARED. Rendering it as $0 would read as "free" or
    "broken", which is finding 4's leak wearing a healthy face. Its resident usage is
    metered exactly like anyone else's, because it still holds a PVC and burns our CPU.
    """
    import asyncio

    from app import metering, portal

    byo = agent_usage.as_usage_row(
        agent_usage.accrue(
            None,
            agent_usage.Observation(
                user="baron", agent="byoagent", pod_uid="u", pod_name="p",
                phase="Running", started_at=_t(SECOND_START), model_source="byo",
                cpu_core_seconds=1800.0, memory_bytes=100_000_000,
                compute_source="kubelet/cadvisor",
            ),
            _t("2026-08-10T04:19:05+00:00")))

    async def go():
        async def _spend(since=None):
            return _spend_rows()

        async def _usage(user=None):
            return [byo]

        original_spend = metering.spend_by_user_and_surface
        original_usage = agent_usage.usage_by_agent
        metering.spend_by_user_and_surface = _spend
        agent_usage.usage_by_agent = _usage
        try:
            return await portal.my_spend(user="baron")
        finally:
            metering.spend_by_user_and_surface = original_spend
            agent_usage.usage_by_agent = original_usage

    entry = asyncio.run(go())["by_agent"][0]
    assert entry["model_source"] == "byo"
    assert entry["inference"]["on_ledger"] is False
    assert "off-ledger by design" in entry["inference"]["note"]
    # Metered all the same. BYO removes the inference row, not the residency row.
    assert entry["usage"]["resident_hours"] == 1.0
    assert entry["usage"]["cpu_core_hours"] == 0.5
