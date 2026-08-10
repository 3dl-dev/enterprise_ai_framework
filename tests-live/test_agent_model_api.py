"""An agent's model API is configurable — integrated metered key, or bring-your-own.

Run against the live k3s cluster:  pytest tests-live/test_agent_model_api.py

Contract 4 of docs/design/records/agents-surface.md, measured rather than reasoned. Two
claims, and neither one can be proven without a real gateway and a real ledger:

  INTEGRATED. `-055` left every agent holding the sentinel
  `unset-pending-enterpriseaiframework-39d`, so its model calls 401 at the gateway on
  purpose. The transition is asserted twice, from two directions. First as a matched pair:
  the SAME request, from the SAME pod, against the SAME gateway, with only the credential
  differing — 401 for the sentinel's exact bytes, 200 for the minted key. Then as the real
  upgrade: an agent's Secret is put back into -055's state, its pod restarted so it truly
  is running on the sentinel and truly does 401, and the real provisioner run again — after
  which the RUNNING pod gets a 200. And then the money shows up on the ONE BILL, read back
  from `/admin/spend`, under `baron / agents/<name>`. That last step is the one that cannot
  be faked: the row is written by LiteLLM into its own Postgres, attributed by the alias,
  and read back by the operator-facing endpoint. Nothing here asserts against a value the
  code under test computed.

  BYO. The user's own provider credential, `OPENAI_API_BASE` pointed away from us. The
  claim is a NEGATIVE one — zero gateway ledger rows — so it is measured the only way a
  negative can be: point a real agent at a real non-gateway endpoint, make a real call
  from inside the pod, confirm something that is not our gateway answered it, and then
  confirm the gateway's ledger gained nothing for that agent.

  ADDITIVITY. `chat`, `ide` and `terminal` are what the camp runs on tomorrow. The whole
  bill is snapshotted before this file does anything and compared afterwards: every base
  surface row that existed must still exist, still be attributed to the same person, and
  still carry at least the spend it had.

COST. The integrated leg makes two real model calls against the cluster's configured
upstream, on the cheapest catalogue entry with max_tokens=8 — fractions of a cent, the
same posture as every other file in tests-live/.

CLEANUP. Everything this file creates is named `agent-baron-swtest39d*`: two Deployments,
their PVCs, Services and Secrets, plus the virtual key `baron::agents/swtest39d` at the
gateway and its row in the control plane. Module teardown removes all of it and runs even
when a test fails. `baron`'s workspace (`ws-baron`) and every other camp fixture are never
touched — an agent is a separate object family.
"""

import json
import subprocess
import time
import uuid

import pytest

NS = "enterprise-ai"
USER = "baron"                       # a REAL principal: a virtual key needs one
INTEGRATED = "swtest39d"
BYO = "swtest39dbyo"
INTEGRATED_OBJ = f"agent-{USER}-{INTEGRATED}"
BYO_OBJ = f"agent-{USER}-{BYO}"

SENTINEL = "unset-pending-enterpriseaiframework-39d"
ALIAS = f"{USER}::agents/{INTEGRATED}"
BYO_ALIAS = f"{USER}::agents/{BYO}"

# The cheapest entry in this cluster's catalogue, with a trivial completion budget.
MODEL = "gemma-3-4b"

# Where a BYO agent's inference is pointed. It has to be somewhere the agent pod can
# actually reach — the NetworkPolicy in 63-agent-common.yaml admits kube-dns, the gateway
# and the MCP tool servers — and it has to be NOT the gateway, which is the whole point.
# mcp-echo is a real HTTP server that is not ours-in-the-billing-sense: it answers, so we
# can prove the request left the pod and was served by something other than the gateway.
BYO_BASE = "http://mcp-echo:8080/v1"


def _run(*args, check=True, timeout=900, **kw):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                          check=check, **kw)


def _kubectl(*args, check=True, timeout=300) -> str:
    return _run("kubectl", "-n", NS, *args, check=check, timeout=timeout).stdout


def _cp(script: str) -> str:
    """Run python inside the control-plane container.

    Used for anything that needs the gateway's admin credential or either database. The
    control plane already holds both and the test host holds neither, so this is the
    access path rather than a convenience — and it means the code doing the reading is the
    DEPLOYED control plane, not a copy of it running here.
    """
    return _run("kubectl", "-n", NS, "exec", "deploy/control-plane", "-c", "control-plane",
                "--", "python3", "-c", script).stdout


def _workspace_image() -> str:
    """The image the Code surface is ACTUALLY running, same as -055's test reads it."""
    image = _kubectl(
        "get", "pod", "-l", "app.kubernetes.io/component=workspace",
        "-o", 'jsonpath={.items[0].spec.containers[?(@.name=="ttyd")].image}',
    ).strip()
    assert image, "no running workspace pod to read the image from"
    return image


def _provision(name: str, *extra: str, env: dict | None = None) -> subprocess.CompletedProcess:
    argv = ["env", f"AGENT_IMAGE={_workspace_image()}"]
    for k, v in (env or {}).items():
        argv.append(f"{k}={v}")
    argv += ["deploy/bin/provision-agent.sh", USER, name, *extra]
    return _run(*argv, check=False, timeout=1200)


def _pod_json(name: str) -> dict:
    out = _kubectl(
        "get", "pod", "-l",
        f"agent.enterprise-ai/name={name},agent.enterprise-ai/user={USER}",
        "-o", "json",
    )
    items = [p for p in json.loads(out)["items"]
             if p["metadata"].get("deletionTimestamp") is None]
    assert items, f"no pod for agent-{USER}-{name}"
    return items[0]


def _wait_ready(name: str, timeout_s=420) -> str:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            pod = _pod_json(name)
        except AssertionError:
            time.sleep(3)
            continue
        if pod["status"].get("phase") == "Running" and all(
            c.get("ready") for c in pod["status"].get("containerStatuses", [])
        ):
            return pod["metadata"]["name"]
        time.sleep(3)
    raise AssertionError(f"agent-{USER}-{name} never became Ready within {timeout_s}s")


def _exec(name: str, script: str) -> subprocess.CompletedProcess:
    return _run("kubectl", "-n", NS, "exec", _pod_json(name)["metadata"]["name"], "--",
                "bash", "-c", script, check=False)


def _call_model_from_pod(name: str, credential: str = "$OPENAI_API_KEY") -> str:
    """Make the agent's OWN model call, from inside the agent's OWN pod.

    The base URL always comes from the pod's environment — the same value opencode uses —
    so this measures the configuration the provisioner actually installed rather than one
    this test chose. `credential` defaults to the pod's own key, which likewise never
    leaves the pod: the shell interpolates it and only the HTTP status comes back.
    """
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 8,
    })
    proc = _exec(name, (
        "curl -sS -m 120 -o /tmp/resp.json -w '%{http_code}' "
        f'-H "Authorization: Bearer {credential}" '
        "-H 'Content-Type: application/json' "
        f"-d '{body}' \"$OPENAI_API_BASE/chat/completions\""
    ))
    return proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else "000"


def _secret_value(secret: str, key: str) -> str:
    import base64
    raw = _kubectl("get", "secret", secret, "-o", f"jsonpath={{.data.{key}}}").strip()
    return base64.b64decode(raw).decode() if raw else ""


def _spend_rows() -> list[dict]:
    """The one bill, from the operator-facing endpoint, over the real gateway ledger."""
    out = _cp(
        "import os,httpx,json;"
        "r=httpx.get('http://localhost:8000/admin/spend',"
        "headers={'Authorization':'Bearer '+os.environ['CONTROL_PLANE_ADMIN_TOKEN']},"
        "timeout=60);"
        "print(json.dumps(r.json()['by_user_and_surface']))"
    )
    return json.loads(out.strip().splitlines()[-1])


def _rows_for(surface: str) -> list[dict]:
    return [r for r in _spend_rows()
            if r.get("username") == USER and r.get("surface") == surface]


def _gateway_aliases(prefix: str) -> list[str]:
    out = _cp(
        "import os,httpx,json;"
        "h={'Authorization':'Bearer '+os.environ['GATEWAY_MASTER_KEY']};"
        "ks=httpx.get('http://gateway:4000/key/list',headers=h,"
        "params={'return_full_object':'true','page':1,'size':100},timeout=60).json()['keys'];"
        "print(json.dumps([k.get('key_alias') for k in ks if isinstance(k,dict)]))"
    )
    aliases = json.loads(out.strip().splitlines()[-1])
    return sorted(a for a in aliases if a and a.startswith(prefix))


def _teardown() -> None:
    """Remove every object this file creates, including the spendable key.

    Order matters for the same reason -055's teardown records: workload first, then the
    PVC, and the PVC deletion is blocked on so that a re-run does not race a Terminating
    volume. The virtual key is revoked separately — an agent's Deployment being gone does
    not stop its key spending money.
    """
    for obj in (INTEGRATED_OBJ, BYO_OBJ):
        _kubectl("delete", "deployment", obj, "--ignore-not-found", "--wait=true",
                 check=False, timeout=300)
        _kubectl("delete", "service", obj, "--ignore-not-found", check=False)
        _kubectl("delete", "secret", f"{obj}-key", "--ignore-not-found", check=False)
        _kubectl("delete", "secret", f"{obj}-byo", "--ignore-not-found", check=False)
        _kubectl("delete", "pvc", obj, "--ignore-not-found", "--wait=true",
                 check=False, timeout=300)

    _run("kubectl", "-n", NS, "exec", "deploy/control-plane", "-c", "control-plane", "--",
         "python3", "-c",
         "import os,asyncio,httpx,asyncpg\n"
         "async def go():\n"
         "    h={'Authorization':'Bearer '+os.environ['GATEWAY_MASTER_KEY']}\n"
         "    httpx.post('http://gateway:4000/key/delete',headers=h,"
         f"json={{'key_aliases':['{ALIAS}','{BYO_ALIAS}']}},timeout=60)\n"
         "    c=await asyncpg.connect(os.environ['CONTROL_PLANE_DATABASE_URL'])\n"
         f"    await c.execute(\"DELETE FROM virtual_key WHERE key_alias = ANY($1)\","
         f"['{ALIAS}','{BYO_ALIAS}'])\n"
         "    await c.close()\n"
         "asyncio.run(go())",
         check=False, timeout=180)

    deadline = time.time() + 240
    while time.time() < deadline:
        remaining = [
            o for o in (INTEGRATED_OBJ, BYO_OBJ)
            if _kubectl("get", "pvc", o, "--ignore-not-found", "-o", "name",
                        check=False).strip()
        ]
        if not remaining:
            return
        time.sleep(3)
    raise AssertionError(f"PVCs still present 240s after delete: {remaining}")


@pytest.fixture(scope="module", autouse=True)
def clean_cluster():
    _teardown()
    try:
        yield
    finally:
        _teardown()


@pytest.fixture(scope="module")
def bill_before(clean_cluster) -> list[dict]:
    """The whole bill, before this file provisions anything. The additivity baseline."""
    return _spend_rows()


# ---------------------------------------------------------------- integrated

@pytest.fixture(scope="module")
def integrated_agent(clean_cluster, bill_before):
    """A fresh integrated agent, provisioned by the real script against the real cluster."""
    proc = _provision(INTEGRATED)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert "(minted)" in proc.stdout, proc.stdout
    _wait_ready(INTEGRATED)
    yield


def test_the_secret_no_longer_holds_the_055_sentinel(integrated_agent):
    held = _secret_value(f"{INTEGRATED_OBJ}-key", "OPENAI_API_KEY")
    assert held != SENTINEL
    assert held.startswith("sk-"), "the Secret does not hold a gateway virtual key"


def test_the_sentinel_401s_and_the_minted_key_200s_from_the_same_pod(integrated_agent):
    """The transition this item exists to make, measured as one pair.

    Same pod, same gateway, same request body, same endpoint — the ONLY thing that differs
    is the credential. The 401 half uses the exact bytes -055 wrote into this Secret, so
    the 200 is not merely "a key works"; it is "the thing that was broken is fixed".

    The 401 is not incidental either: it is `deploy/gateway/require_principal.py` refusing
    a credential the ledger could not name, which is the property that makes every number
    on the bill attributable.
    """
    assert _call_model_from_pod(INTEGRATED, credential=SENTINEL) == "401", (
        "the -055 sentinel did NOT 401 at the gateway. Either the gateway stopped "
        "requiring an attributable principal or this agent is not pointed at the gateway "
        "at all — both make the 200 below meaningless."
    )

    status = _call_model_from_pod(INTEGRATED)
    assert status == "200", (
        f"the agent's model call returned {status} with its minted key. "
        f"Response: {_exec(INTEGRATED, 'cat /tmp/resp.json').stdout[:400]}"
    )


def test_the_alias_at_the_gateway_is_contract_ones_grammar(integrated_agent):
    """Read off the GATEWAY's own key list, not off our code.

    One `::`, the instance folded into the surface field. A third separator here is the
    silent defect Contract 1 exists to prevent: Python and SQL would then disagree about
    who spent the money, in different directions, with nothing erroring.
    """
    aliases = _gateway_aliases(f"{USER}::agents/")
    assert aliases == [ALIAS], aliases
    assert ALIAS.count("::") == 1


def test_the_spend_lands_on_the_one_bill_under_the_agents_surface(integrated_agent):
    """The row, read back from /admin/spend over the real gateway ledger.

    LiteLLM writes spend rows asynchronously, so this polls rather than assuming the row
    is there the instant the response returned. `agents/<name>` — the per-instance surface
    — is what makes an agent's inference legible per agent on the bill for free, with no
    change to `metering.spend_by_user_and_surface`.
    """
    deadline = time.time() + 180
    rows: list[dict] = []
    while time.time() < deadline:
        rows = _rows_for(f"agents/{INTEGRATED}")
        if rows:
            break
        time.sleep(5)

    assert rows, (
        f"no ledger row for {USER} / agents/{INTEGRATED} within 180s. The call returned "
        "200, so the money was spent; a bill that cannot name it is the failure this "
        "project exists to prevent."
    )
    row = rows[0]
    assert row["requests"] >= 1, row
    assert row["prompt_tokens"] > 0, row
    assert row["username"] == USER, row
    assert row["surface"] == f"agents/{INTEGRATED}", row


def test_an_agent_left_holding_the_sentinel_is_upgraded_and_the_pod_picks_it_up(
    integrated_agent,
):
    """The upgrade path for an agent -055 already provisioned, end to end.

    The Secret is put back into exactly the state -055 shipped, the pod is restarted so it
    really is running on the sentinel, and then the real provisioner is run again. Two
    things have to happen and BOTH are load-bearing: it must mint (not shrug at a Secret
    that already has a value), and the POD must end up holding the new key — env from a
    secretKeyRef is injected at pod start and never updated, so a provisioner that changed
    only the Secret would leave a resident agent 401ing forever with a correct key sitting
    next to it.
    """
    import base64
    encoded = base64.b64encode(SENTINEL.encode()).decode()
    _kubectl("patch", "secret", f"{INTEGRATED_OBJ}-key", "-p",
             json.dumps({"data": {"OPENAI_API_KEY": encoded}}))
    _kubectl("rollout", "restart", f"deployment/{INTEGRATED_OBJ}")
    _kubectl("rollout", "status", f"deployment/{INTEGRATED_OBJ}", "--timeout=600s")
    _wait_ready(INTEGRATED)
    assert _call_model_from_pod(INTEGRATED) == "401", (
        "the pod is supposed to be running on the sentinel here; it is not, so the "
        "upgrade measured below would prove nothing."
    )

    proc = _provision(INTEGRATED)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert "(minted)" in proc.stdout, proc.stdout
    assert _secret_value(f"{INTEGRATED_OBJ}-key", "OPENAI_API_KEY") != SENTINEL

    _kubectl("rollout", "status", f"deployment/{INTEGRATED_OBJ}", "--timeout=600s")
    _wait_ready(INTEGRATED)
    assert _call_model_from_pod(INTEGRATED) == "200", (
        "the Secret was upgraded but the running pod is still 401ing — the key never "
        f"reached the process. Response: {_exec(INTEGRATED, 'cat /tmp/resp.json').stdout[:400]}"
    )


def test_reprovisioning_a_healthy_agent_neither_rotates_nor_restarts_it(integrated_agent):
    """Non-disruptive, measured on the real pod.

    Restarting an agent ends the resident session that is the entire product. So a re-run
    for an agent that already holds a working key must be a no-op: same key in the Secret,
    same pod, same container, no restart.
    """
    before_key = _secret_value(f"{INTEGRATED_OBJ}-key", "OPENAI_API_KEY")
    before_pod = _pod_json(INTEGRATED)
    proc = _provision(INTEGRATED)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    assert "(kept" in proc.stdout, proc.stdout

    after_pod = _pod_json(INTEGRATED)
    assert _secret_value(f"{INTEGRATED_OBJ}-key", "OPENAI_API_KEY") == before_key
    assert after_pod["metadata"]["name"] == before_pod["metadata"]["name"], (
        "the pod was replaced by a no-op re-provision, which ends the resident session"
    )
    assert (after_pod["status"]["containerStatuses"][0]["restartCount"]
            == before_pod["status"]["containerStatuses"][0]["restartCount"])


# ---------------------------------------------------------------- BYO

def _stop(name: str) -> None:
    """Contract 2's `running -> stopped`: replicas 0, PVC retained, zero resident cost."""
    obj = f"agent-{USER}-{name}"
    _kubectl("scale", f"deployment/{obj}", "--replicas=0")
    deadline = time.time() + 240
    while time.time() < deadline:
        out = _kubectl("get", "pod", "-l",
                       f"agent.enterprise-ai/name={name},agent.enterprise-ai/user={USER}",
                       "-o", "name", check=False).strip()
        if not out:
            return
        time.sleep(3)
    raise AssertionError(f"{obj} still has a pod 240s after scaling to zero")


@pytest.fixture(scope="module")
def byo_agent(clean_cluster, bill_before, integrated_agent, tmp_path_factory):
    """A BYO agent, provisioned after the integrated one has been STOPPED.

    Two agents do not run at once here, and that is a property of this cluster rather than
    of the surface: k3s-worker also runs live GPU training and sits at ~94% of its CPU
    requests with one workspace-sized agent up, so a second would sit Pending forever and
    this file would report a scheduling problem as a BYO defect. Stopping the first one is
    Contract 2's own `running -> stopped` transition — replicas 0, PVC retained — so the
    integrated agent's ledger row, key and state all survive for the assertions that
    follow, and nothing here is a workaround for a behaviour under test.
    """
    _stop(INTEGRATED)
    key_file = tmp_path_factory.mktemp("byo") / "key"
    # A throwaway value standing in for a user's own provider credential. It is never
    # supposed to reach our gateway, and nothing here ever prints it.
    key_file.write_text("sk-byo-" + uuid.uuid4().hex + "\n")
    proc = _provision(BYO, "--byo-key-file", str(key_file), "--byo-api-base", BYO_BASE)
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    _wait_ready(BYO)
    yield key_file.read_text().strip()


def test_a_byo_agent_is_pointed_away_from_our_gateway(byo_agent):
    """Read off the running pod's own environment."""
    env = {e["name"]: e for e in
           _pod_json(BYO)["spec"]["containers"][0]["env"]}
    assert env["OPENAI_API_BASE"]["value"] == BYO_BASE
    assert "gateway:4000" not in env["OPENAI_API_BASE"]["value"]
    assert env["OPENAI_API_KEY"]["valueFrom"]["secretKeyRef"]["name"] == f"{BYO_OBJ}-byo"


def test_no_virtual_key_is_minted_for_a_byo_agent(byo_agent):
    """The cause of the zero rows, checked at the gateway itself."""
    assert BYO_ALIAS not in _gateway_aliases(f"{USER}::agents/")


def test_a_byo_agent_is_declared_and_therefore_never_a_silent_zero(byo_agent):
    """Contract 4's condition for allowing an off-gateway path at all.

    Finding 4's leak was off-ledger by ACCIDENT, on a shared surface, rendered as healthy.
    This is off-ledger by DECLARATION, and the declaration is a property of the object that
    -627's tab and -914's meter read — not a note in a document.
    """
    labels = _pod_json(BYO)["metadata"]["labels"]
    assert labels["agent.enterprise-ai/model-source"] == "byo"
    # And the residency attribution survives: BYO removes the inference row, not the
    # resident-time row. The pod still holds a PVC and burns CPU on our hardware.
    assert labels["agent.enterprise-ai/user"] == USER
    assert labels["agent.enterprise-ai/name"] == BYO


def test_a_byo_call_leaves_the_pod_and_lands_no_gateway_ledger_row(byo_agent):
    """The negative claim, measured.

    The pod makes a real request to its configured base with its configured credential.
    Something that is not the gateway answers it — a real HTTP status, not a connection
    failure, is what proves the request actually went somewhere — and afterwards the
    gateway's ledger has gained nothing at all for this agent.
    """
    status = _call_model_from_pod(BYO)
    assert status != "000", (
        "the BYO agent's request never got an HTTP response, so this test has not shown "
        "where the traffic went. Check the egress allowlist in 63-agent-common.yaml."
    )

    # Generous: LiteLLM's spend flush is asynchronous, so a row that WAS going to be
    # written has plenty of time to appear before this concludes it never will.
    time.sleep(45)
    assert _rows_for(f"agents/{BYO}") == [], (
        "a BYO agent produced a gateway ledger row. Its inference is supposed to route "
        "around this layer entirely on the user's own credential."
    )
    assert not [r for r in _spend_rows() if r.get("surface", "").endswith(f"/{BYO}")]


def test_the_byo_credential_is_not_returned_by_any_key_listing(byo_agent):
    """Set-once. There is no read path, and this checks the two that exist for keys.

    The gateway's key list is what `/portal/api/keys` renders; the control plane's own
    key table is what `/admin/keys` renders. Neither can contain a credential we never
    minted and never stored anywhere but a Secret the pod alone mounts.
    """
    byo_key = byo_agent
    listing = _cp(
        "import os,httpx,json;"
        "h={'Authorization':'Bearer '+os.environ['GATEWAY_MASTER_KEY']};"
        "print(httpx.get('http://gateway:4000/key/list',headers=h,"
        "params={'return_full_object':'true','page':1,'size':100},timeout=60).text)"
    )
    assert byo_key not in listing

    admin_keys = _cp(
        "import os,httpx;"
        "print(httpx.get('http://localhost:8000/admin/keys',"
        "headers={'Authorization':'Bearer '+os.environ['CONTROL_PLANE_ADMIN_TOKEN']},"
        "timeout=60).text)"
    )
    assert byo_key not in admin_keys
    assert BYO_ALIAS not in admin_keys


# ---------------------------------------------------------------- additivity

def test_the_existing_surfaces_bill_exactly_as_they_did_before(
    bill_before, integrated_agent, byo_agent
):
    """The camp's surfaces, before and after, over the real ledger.

    `chat`, `ide` and `terminal` are what runs tomorrow. Adding a fourth surface family to
    the alias grammar is worth nothing if it perturbs how the existing three are
    attributed — and "we only added things" is a promise until the bill is compared.
    """
    after = {(r["username"], r["surface"]): r
             for r in _spend_rows() if "/" not in r.get("surface", "")}

    for row in bill_before:
        surface = row.get("surface", "")
        if "/" in surface:            # an agents row from a previous item, not a base one
            continue
        key = (row["username"], surface)
        assert key in after, (
            f"{key} was on the bill before this file ran and is not on it now. The agents "
            "surface changed how an existing surface is attributed."
        )
        assert after[key]["requests"] >= row["requests"], (row, after[key])
        assert after[key]["spend"] >= row["spend"] - 1e-9, (row, after[key])


def test_the_agents_rows_are_additional_and_did_not_replace_anything(
    bill_before, integrated_agent
):
    """The new row is a NEW row. `agents/<name>` did not fold into any existing surface."""
    base_surfaces = {r["surface"] for r in bill_before if "/" not in r.get("surface", "")}
    assert f"agents/{INTEGRATED}" not in base_surfaces
    assert _rows_for(f"agents/{INTEGRATED}"), "the agents row vanished"
    after_base = {r["surface"] for r in _spend_rows() if "/" not in r.get("surface", "")}
    assert base_surfaces <= after_base, (base_surfaces - after_base)
