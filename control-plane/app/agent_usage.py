"""The second metering dimension: how long an agent was resident, and what it burned.

WHAT THIS MEASURES, AND WHAT IT DELIBERATELY DOES NOT

Contract 3 of docs/design/records/agents-surface.md names two dimensions. The first —
inference tokens — is already metered, by the gateway, and read back by `metering.py`.
This module is the second one, and it is the dimension the token ledger cannot see: an
agent that is *up* holds a PVC, occupies a scheduling slot and burns CPU whether or not it
is calling a model. A BYO agent (Contract 4) produces no gateway ledger row at all and
still does every one of those things.

**Baron's ruling, and it is normative here: METER USAGE, NOT COST.** The hardware is
owned, so its cost is sunk; inference already tracks a real Forge cost, and compute on
owned hardware does not have one. So what this module produces is a QUANTITY — hours,
CPU-core-hours, megabytes — and there is no dollar figure, no rate, and no pricing
configuration anywhere in it. The design record's Contract 3b recommended a resident-hour
plus core-hour *rate*; that recommendation was RESERVED to Baron and he ruled against it.
If commodity cloud compute is ever added, cost-wiring is a future item: the quantities
below are the seam it would multiply, and building the multiplier now would be inventing a
number nobody owes.

WHY IT IS A SEPARATE LEDGER

It lives in the control-plane database, in its own table, and it is composed with
inference spend in the ENDPOINT layer (`portal.py`, `main.py`) — never in SQL. So
`metering.spend_by_user_and_surface` is byte-unchanged and `/admin/spend` returns exactly
what it returned before this landed. That is asserted mechanically by
control-plane/tests/test_agent_usage.py, because "we didn't touch the bill" is a promise
until something checks it. The operator's bill must not move because a usage meter shipped.

THE DATA SOURCES, AND WHY THESE

* Resident time: the pod's own `.status.startTime`, read from the Kubernetes API. A pod
  that is `Running` is accruing. `stopped` is `replicas: 0` (Contract 2), so there is no
  pod, so there is nothing to sample and the number FREEZES — not "billed at a stopped
  rate", literally no observation.

* Compute: cAdvisor's `container_cpu_usage_seconds_total`, scraped from the kubelet the
  agent's pod is actually running on (`.status.hostIP`), plus
  `container_memory_working_set_bytes` for a high-water mark. It is a monotonic COUNTER,
  which is the whole reason the design record chose it over metrics-server: a collector
  that misses a sample under-reports nothing, because the next read still sees the full
  delta. metrics-server exposes an instantaneous gauge, so a missed sample is lost
  forever, and a quantity nobody can reconcile is worse than no quantity at all.

  If the counter cannot be read — no RBAC, no route, a kubelet that does not serve it —
  this records `compute_source = None` and leaves the compute quantity where it was.
  It never substitutes a guess. Resident time is independent and keeps working.

HOW THE ACCUMULATION IS SAFE

`accrue()` below is a pure function of (what we last recorded, one observation, now). It
is the whole meter, and it is separated from both the cluster and the database so it can
be tested against REAL recorded observations rather than against itself. Two properties it
has to have and does:

  * It only ever adds. There is no path that recomputes a total from scratch, so a
    collector restart, a missed tick, or a pod replacement cannot make a total go down.
  * It is keyed on the pod UID, so a NEW pod incarnation starts its CPU delta from zero
    (cAdvisor's counter restarts with the container) while the SAME incarnation deltas
    against the last reading. Getting that backwards double-counts every restart.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

import httpx

from . import db

log = logging.getLogger("agent_usage")

# The in-cluster service account credential. Read from disk on EVERY call rather than
# cached at import: projected service-account tokens are rotated by the kubelet (hourly by
# default), and a process holding the boot-time value starts 401ing after an hour with
# nothing on screen to say why.
SA_DIR = Path(os.environ.get(
    "KUBE_SA_DIR", "/var/run/secrets/kubernetes.io/serviceaccount"
))
TOKEN_FILE = SA_DIR / "token"
CA_FILE = SA_DIR / "ca.crt"
NAMESPACE_FILE = SA_DIR / "namespace"

# The Kubernetes API, from inside the cluster.
KUBE_API = "https://{}:{}".format(
    os.environ.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc"),
    os.environ.get("KUBERNETES_SERVICE_PORT", "443"),
)

# The kubelet's own metrics port. Scraped directly at the node the pod runs on, addressed
# by `.status.hostIP`, so no permission to LIST NODES is needed — the pod read already
# carries the node address. Going through the API server instead would need
# `nodes/proxy`, which grants the whole kubelet API (exec included) and is a privilege
# escalation this meter has no business holding. See deploy/k8s/39-control-plane-rbac.yaml.
KUBELET_PORT = int(os.environ.get("AGENT_USAGE_KUBELET_PORT", "10250"))

# How often the collector samples. Resident time is accrued between ticks, so a longer
# interval is not less accurate for a pod that stays up — it only widens the window that
# is lost when a pod disappears between two ticks. CPU is a counter and loses nothing.
SAMPLE_SECONDS = float(os.environ.get("AGENT_USAGE_SAMPLE_SECONDS", "60"))

# Labels are the attribution key, per Contract 3: compute is consumed by the POD, not by
# an inference call, so attribution comes from the object that consumed it.
COMPONENT_SELECTOR = "app.kubernetes.io/component=agent"
USER_LABEL = "agent.enterprise-ai/user"
NAME_LABEL = "agent.enterprise-ai/name"
MODEL_SOURCE_LABEL = "agent.enterprise-ai/model-source"
# The Agents-pillar type dimension (agents-gateway-console.md Contract A): the harness
# an instance runs. Carried as a pod label exactly like model-source above, so the console
# proxy (-8e4) and model API (-840) can select the per-type native port/API shape from the
# label rather than re-deriving it. `hermes` is the incumbent default; `openclaw` is the
# sibling. `opencode` is deliberately NOT a value — that is the Code pillar, not an agent.
TYPE_LABEL = "agent.enterprise-ai/type"
AGENT_TYPES = ("hermes", "openclaw")
DEFAULT_AGENT_TYPE = "hermes"

CPU_METRIC = "container_cpu_usage_seconds_total"
MEMORY_METRIC = "container_memory_working_set_bytes"

# What the ledger records as the provenance of a compute number, so a reader can tell a
# real measurement from an absent one without inferring it from a zero.
SOURCE_CADVISOR = "kubelet/cadvisor"


def enabled() -> bool:
    """Whether this deployment can meter at all.

    False in the compose bundle, which has no Kubernetes and therefore no agents. The
    table still exists there and reads still answer — with nothing in it, which is the
    truth — rather than the endpoint erroring.
    """
    override = os.environ.get("AGENT_USAGE_ENABLED")
    if override is not None:
        return override.strip().lower() in ("1", "true", "yes", "on")
    return TOKEN_FILE.is_file()


def namespace() -> str:
    try:
        return NAMESPACE_FILE.read_text().strip() or "enterprise-ai"
    except OSError:
        return os.environ.get("AGENT_NAMESPACE", "enterprise-ai")


# ---------------------------------------------------------------- the observation


@dataclass(frozen=True)
class Observation:
    """One agent pod, as the cluster describes it at one instant.

    `cpu_core_seconds` is the cAdvisor COUNTER for this pod — cumulative since the
    container started — not a delta. `None` means it could not be read, which is different
    from zero and is carried as such all the way to the endpoint.
    """

    user: str
    agent: str
    pod_uid: str
    pod_name: str
    phase: str
    started_at: datetime | None
    node_ip: str = ""
    model_source: str = ""
    cpu_core_seconds: float | None = None
    memory_bytes: int | None = None
    compute_source: str | None = None

    @property
    def running(self) -> bool:
        return self.phase == "Running"


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def observation_from_pod(pod: dict) -> Observation | None:
    """Read one pod object into an observation, or None if it is not an agent.

    A pod missing either half of the attribution key is skipped rather than attributed to
    a blank user: a usage row nobody owns is worse than a missing one, because it looks
    like somebody's.
    """
    meta = pod.get("metadata") or {}
    labels = meta.get("labels") or {}
    user = (labels.get(USER_LABEL) or "").strip()
    agent = (labels.get(NAME_LABEL) or "").strip()
    if not user or not agent:
        return None
    status = pod.get("status") or {}
    return Observation(
        user=user,
        agent=agent,
        pod_uid=meta.get("uid") or "",
        pod_name=meta.get("name") or "",
        phase=status.get("phase") or "",
        started_at=_parse_time(status.get("startTime")),
        node_ip=status.get("hostIP") or "",
        model_source=(labels.get(MODEL_SOURCE_LABEL) or "").strip(),
    )


# ---------------------------------------------------------------- the meter itself


def accrue(previous: dict | None, obs: Observation, now: datetime) -> dict:
    """Fold one observation into the durable counters. Pure: no clock, no cluster, no DB.

    THE THREE RULES, each of which has a failure mode it exists to prevent:

    1. Resident time accrues only while the pod is Running, and only forward. For the
       SAME pod incarnation the interval is `now - last_observed_at`, so a collector that
       was down for an hour while the pod was up still credits that hour. For a NEW
       incarnation it is `now - startTime`, which is the only correct answer when we have
       never seen this pod before. Taking `now - startTime` unconditionally would
       re-credit the pod's entire life on every single tick.

    2. CPU deltas against the last reading OF THE SAME POD UID. cAdvisor's counter is
       cumulative per container, so it restarts at zero with a new container: a new UID
       contributes its whole counter, and a counter that went BACKWARDS within one UID
       (an in-place container restart) likewise contributes its whole current value.
       Deltaing across a restart without that check yields a large negative number, and a
       meter that can go down is not a meter.

    3. A failed compute read (`cpu_core_seconds is None`) carries the previous counter
       forward UNCHANGED — it does not reset `last_cpu_counter`. That is what makes a
       missed sample cost nothing: the next successful read deltas against the last
       successful one and recovers the whole interval. This is the entire reason the
       design record chose a counter over metrics-server's gauge.
    """
    prev = previous or {}
    same_incarnation = bool(obs.pod_uid) and prev.get("last_pod_uid") == obs.pod_uid

    resident = float(prev.get("resident_seconds") or 0.0)
    if obs.running and obs.started_at is not None:
        last_seen = prev.get("last_observed_at") if same_incarnation else None
        anchor = max(last_seen, obs.started_at) if last_seen else obs.started_at
        resident += max((now - anchor).total_seconds(), 0.0)

    cpu_total = float(prev.get("cpu_core_seconds") or 0.0)
    last_counter = prev.get("last_cpu_counter")
    if obs.cpu_core_seconds is not None:
        if same_incarnation and last_counter is not None and obs.cpu_core_seconds >= last_counter:
            cpu_total += obs.cpu_core_seconds - float(last_counter)
        else:
            cpu_total += obs.cpu_core_seconds
        last_counter = obs.cpu_core_seconds

    peak = int(prev.get("memory_peak_bytes") or 0)
    if obs.memory_bytes is not None:
        peak = max(peak, int(obs.memory_bytes))

    return {
        "agent_user": obs.user,
        "agent_name": obs.agent,
        "resident_seconds": resident,
        "cpu_core_seconds": cpu_total,
        "memory_peak_bytes": peak,
        # Provenance travels with the number. A `compute_source` of None next to a
        # cpu_core_seconds of 0 says "not measured"; SOURCE_CADVISOR next to 0 says
        # "measured, and it really was idle". Collapsing those two into a bare zero is
        # how an unmetered path comes to look healthy (finding 4).
        "compute_source": obs.compute_source or prev.get("compute_source"),
        "model_source": obs.model_source or prev.get("model_source"),
        "last_pod_uid": obs.pod_uid,
        "last_pod_name": obs.pod_name,
        "last_pod_phase": obs.phase,
        "last_cpu_counter": last_counter,
        "last_observed_at": now,
    }


# ---------------------------------------------------------------- reading the cluster


def _token() -> str:
    return TOKEN_FILE.read_text().strip()


def _verify():
    """TLS trust for both endpoints, and it is a real chain, not `verify=False`.

    The kubelet's serving certificate on this cluster is signed by the same CA the service
    account bundle carries, so scraping it verifies exactly like an API-server call does.
    Measured, not assumed — tests-live/test_agent_usage.py fails if the verified scrape
    stops working, rather than quietly falling back to an unverified one. A meter that
    disables certificate verification to keep working is a MITM away from attributing
    somebody else's numbers.
    """
    return str(CA_FILE) if CA_FILE.is_file() else True


async def list_agent_pods(client: httpx.AsyncClient) -> list[Observation]:
    """Every agent pod in the namespace, as observations. Needs only namespaced pod read."""
    resp = await client.get(
        f"{KUBE_API}/api/v1/namespaces/{namespace()}/pods",
        params={"labelSelector": COMPONENT_SELECTOR},
        headers={"Authorization": f"Bearer {_token()}"},
        timeout=20.0,
    )
    resp.raise_for_status()
    out = []
    for pod in resp.json().get("items", []):
        obs = observation_from_pod(pod)
        if obs is not None:
            out.append(obs)
    return out


def parse_cadvisor(text: str, ns: str) -> dict[str, dict]:
    """Pull the two cAdvisor series we meter out of a kubelet's Prometheus exposition.

    Returns `{pod_name: {"cpu_core_seconds": float, "memory_bytes": int}}`, summed over
    the pod's own containers.

    Lines with `container=""` are SKIPPED on purpose. That series is the pod cgroup, which
    includes the pause container's overhead — real, but not the agent's work, and counting
    both it and the per-container series would double the number. Summing the named
    containers is the quantity the design record specifies and the one a person can check
    by hand against `kubectl top`.
    """
    out: dict[str, dict] = {}
    for line in text.splitlines():
        if not (line.startswith(CPU_METRIC) or line.startswith(MEMORY_METRIC)):
            continue
        name, _, rest = line.partition("{")
        labels_text, _, value_text = rest.partition("}")
        if not value_text:
            continue
        labels = _parse_labels(labels_text)
        if labels.get("namespace") != ns or not labels.get("container"):
            continue
        pod = labels.get("pod")
        if not pod:
            continue
        # `container_cpu_usage_seconds_total` is exported per cpu as well as for
        # `cpu="total"`; taking both would multiply the number by the core count.
        if name == CPU_METRIC and labels.get("cpu") not in (None, "total"):
            continue
        try:
            value = float(value_text.split()[0])
        except (ValueError, IndexError):
            continue
        row = out.setdefault(pod, {"cpu_core_seconds": 0.0, "memory_bytes": 0})
        if name == CPU_METRIC:
            row["cpu_core_seconds"] += value
        else:
            row["memory_bytes"] += int(value)
    return out


def _parse_labels(text: str) -> dict[str, str]:
    """`k="v",k2="v2"` -> dict, honouring the escapes the Prometheus text format defines.

    Hand-written rather than regex'd because cAdvisor's `id=` label is a cgroup path full
    of the characters a naive split would break on, and a label parser that mis-splits one
    line silently attributes that line's CPU to the wrong pod.
    """
    out: dict[str, str] = {}
    i, n = 0, len(text)
    while i < n:
        eq = text.find('="', i)
        if eq < 0:
            break
        key = text[i:eq].lstrip(", ")
        i = eq + 2
        buf = []
        while i < n:
            c = text[i]
            if c == "\\" and i + 1 < n:
                nxt = text[i + 1]
                buf.append({"n": "\n", "\\": "\\", '"': '"'}.get(nxt, nxt))
                i += 2
                continue
            if c == '"':
                i += 1
                break
            buf.append(c)
            i += 1
        out[key] = "".join(buf)
    return out


async def read_compute(client: httpx.AsyncClient, node_ip: str, ns: str) -> dict[str, dict]:
    """Scrape one kubelet's cAdvisor endpoint. Returns {} — never a fake zero — on failure."""
    try:
        resp = await client.get(
            f"https://{node_ip}:{KUBELET_PORT}/metrics/cadvisor",
            headers={"Authorization": f"Bearer {_token()}"},
            timeout=30.0,
        )
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - the reason is logged and the meter degrades
        log.warning("cadvisor scrape of %s failed: %s", node_ip, exc)
        return {}
    return parse_cadvisor(resp.text, ns)


async def observe() -> list[Observation]:
    """One full sample of the cluster: every agent pod, with compute where it is readable.

    Compute failing does not fail the sample. Resident time is the must-have and it comes
    from the pod object alone; the compute half degrades to `compute_source = None`, which
    the endpoints render as "not measured" instead of as zero.
    """
    ns = namespace()
    async with httpx.AsyncClient(verify=_verify()) as client:
        pods = await list_agent_pods(client)
        node_ips = {p.node_ip for p in pods if p.node_ip and p.running}
        metrics: dict[str, dict] = {}
        for ip in sorted(node_ips):
            metrics.update(await read_compute(client, ip, ns))

    out = []
    for pod in pods:
        measured = metrics.get(pod.pod_name)
        if measured is not None:
            pod = replace(
                pod,
                cpu_core_seconds=measured["cpu_core_seconds"],
                memory_bytes=measured["memory_bytes"],
                compute_source=SOURCE_CADVISOR,
            )
        out.append(pod)
    return out


# ---------------------------------------------------------------- the ledger


_COLUMNS = (
    "agent_user", "agent_name", "resident_seconds", "cpu_core_seconds",
    "memory_peak_bytes", "compute_source", "model_source", "last_pod_uid",
    "last_pod_name", "last_pod_phase", "last_cpu_counter", "last_observed_at",
)


async def _load(conn, user: str, agent: str) -> dict | None:
    row = await conn.fetchrow(
        f"SELECT {', '.join(_COLUMNS)} FROM agent_usage "
        "WHERE agent_user = $1 AND agent_name = $2",
        user, agent,
    )
    return dict(row) if row else None


async def record(observations: list[Observation], now: datetime | None = None) -> int:
    """Fold a sample into the ledger. Returns how many agent rows moved.

    One transaction per agent, not one for the whole sample: a single unparseable pod must
    not roll back the hours of every other agent in the namespace.
    """
    now = now or datetime.now(timezone.utc)
    pool = await db.pool()
    written = 0
    for obs in observations:
        async with pool.acquire() as conn:
            async with conn.transaction():
                # Locked for the read-modify-write. The collector and a forced collect can
                # run at once, and two folds racing on the same row would each add their
                # own interval to the same starting total — silently doubling it.
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext($1))",
                    f"agent_usage:{obs.user}/{obs.agent}",
                )
                previous = await _load(conn, obs.user, obs.agent)
                folded = accrue(previous, obs, now)
                await conn.execute(
                    """
                    INSERT INTO agent_usage (
                        agent_user, agent_name, resident_seconds, cpu_core_seconds,
                        memory_peak_bytes, compute_source, model_source, last_pod_uid,
                        last_pod_name, last_pod_phase, last_cpu_counter, last_observed_at
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
                    ON CONFLICT (agent_user, agent_name) DO UPDATE SET
                        resident_seconds  = EXCLUDED.resident_seconds,
                        cpu_core_seconds  = EXCLUDED.cpu_core_seconds,
                        memory_peak_bytes = EXCLUDED.memory_peak_bytes,
                        compute_source    = EXCLUDED.compute_source,
                        model_source      = EXCLUDED.model_source,
                        last_pod_uid      = EXCLUDED.last_pod_uid,
                        last_pod_name     = EXCLUDED.last_pod_name,
                        last_pod_phase    = EXCLUDED.last_pod_phase,
                        last_cpu_counter  = EXCLUDED.last_cpu_counter,
                        last_observed_at  = EXCLUDED.last_observed_at
                    """,
                    *[folded[c] for c in _COLUMNS],
                )
                written += 1
    return written


# What the ledger records as a phase for an agent that has no pod at all. It is not a
# Kubernetes phase — Kubernetes has nothing to say about an object that is gone — and it is
# spelled distinctly so it can never be confused with one.
PHASE_ABSENT = "Absent"


async def mark_absent(observations: list[Observation]) -> int:
    """Say so when an agent has stopped, WITHOUT touching a single counter.

    `stopped` is `replicas: 0`, so the pod is gone and the meter simply stops observing it
    — which is exactly what freezes the totals. But freezing the totals is not enough on
    its own: the last row we wrote still said `phase: Running`, so a stopped agent went on
    rendering as running forever, with a resident-hours figure that had quietly stopped
    moving. That is the worst of both readings, and it is what a page would show a user.

    So a sample that SUCCEEDED also records which agents it did not see. Only the phase
    column is written. The counters, the CPU bookmark and `last_observed_at` are all left
    exactly where they were, because touching `last_observed_at` here would make the next
    start-up credit the stopped interval as resident time.

    Called only after a successful list. A failed API read raises before this point, so a
    momentary outage can never mark a running fleet as stopped.
    """
    seen = {(o.user, o.agent) for o in observations}
    pool = await db.pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT agent_user, agent_name FROM agent_usage "
            "WHERE last_pod_phase IS DISTINCT FROM $1",
            PHASE_ABSENT,
        )
        gone = [(r["agent_user"], r["agent_name"]) for r in rows
                if (r["agent_user"], r["agent_name"]) not in seen]
        for user, agent in gone:
            await conn.execute(
                "UPDATE agent_usage SET last_pod_phase = $3 "
                "WHERE agent_user = $1 AND agent_name = $2",
                user, agent, PHASE_ABSENT,
            )
    return len(gone)


async def collect_once() -> dict:
    """Sample the cluster and fold it in. The unit the timer runs and /admin can force."""
    if not enabled():
        return {"enabled": False, "agents": 0, "compute_measured": 0}
    observations = await observe()
    await record(observations)
    await mark_absent(observations)
    return {
        "enabled": True,
        "agents": len(observations),
        "running": sum(1 for o in observations if o.running),
        "compute_measured": sum(1 for o in observations if o.compute_source),
    }


def as_usage_row(row: dict) -> dict:
    """One ledger row rendered as USAGE. No dollars — see the module docstring.

    Both the raw counters and the human units are returned. The raw seconds are what a
    later reconciliation would work from; the hours are what a page renders. Deriving one
    from the other in a renderer is how two views come to disagree about the same number.
    """
    resident = float(row.get("resident_seconds") or 0.0)
    cpu = float(row.get("cpu_core_seconds") or 0.0)
    peak = int(row.get("memory_peak_bytes") or 0)
    return {
        "user": row["agent_user"],
        "agent": row["agent_name"],
        # The SAME string the inference ledger uses for this agent's spend (Contract 1),
        # so a caller can line the two dimensions up without parsing anything.
        "surface": f"agents/{row['agent_name']}",
        "model_source": row.get("model_source") or "",
        "resident_seconds": round(resident, 3),
        "resident_hours": round(resident / 3600.0, 6),
        "cpu_core_seconds": round(cpu, 3),
        "cpu_core_hours": round(cpu / 3600.0, 6),
        "memory_peak_bytes": peak,
        "memory_peak_mb": round(peak / 1_000_000.0, 3),
        "compute_source": row.get("compute_source"),
        # False here is the difference between "measured, and idle" and "never measured".
        "compute_measured": bool(row.get("compute_source")),
        "running": (row.get("last_pod_phase") or "") == "Running",
        # `Absent` means scaled to zero (Contract 2's `stopped`): no pod, nothing to
        # sample, and the quantities above are therefore final until it starts again.
        "pod_phase": row.get("last_pod_phase") or "",
        "last_observed_at": (
            row["last_observed_at"].isoformat() if row.get("last_observed_at") else None
        ),
    }


async def usage_by_agent(user: str | None = None) -> list[dict]:
    """The usage ledger, optionally scoped to one owner. Quantities only."""
    sql = f"SELECT {', '.join(_COLUMNS)} FROM agent_usage"
    args: list = []
    if user is not None:
        sql += " WHERE agent_user = $1"
        args.append(user)
    sql += " ORDER BY agent_user, agent_name"
    pool = await db.pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)
    return [as_usage_row(dict(r)) for r in rows]


# ---------------------------------------------------------------- the timer


async def _loop() -> None:
    while True:
        try:
            await collect_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            # A meter that takes the control plane down with it is worse than a meter that
            # misses an interval — and because the CPU source is a counter, the next tick
            # recovers the compute it missed. Resident time loses only the gap.
            log.warning("agent usage collection failed: %s", exc)
        await asyncio.sleep(SAMPLE_SECONDS)


def start_collector() -> asyncio.Task | None:
    if not enabled():
        log.info("agent usage collector disabled (no in-cluster credential)")
        return None
    return asyncio.create_task(_loop(), name="agent-usage-collector")


async def stop_collector(task: asyncio.Task | None) -> None:
    if task is None:
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass
