"""An agent's model API is configurable: integrated metered key, or bring-your-own.

Contract 4 of docs/design/records/agents-surface.md, tested through the real provisioner
(see tests/agent_provision_harness.py for what is faked — the cluster — and what is not —
the script). The claims are:

  INTEGRATED (default): a per-agent virtual key is minted through the CONTROL PLANE with
    Contract 1's `<user>::agents/<name>` alias, replacing -055's sentinel, and the pod
    points at our gateway. Metered, budgeted, on the one bill.

  BYO: the user's own provider credential goes into a separate Secret, the pod points at
    THEIR provider, and NO virtual key is minted — so the gateway ledger gets nothing for
    this agent, by declaration rather than by accident. The pod is labelled
    `model-source: byo` so no spend view can render it as a silent $0.

  The BYO key is the sensitive surface: never echoed, never in argv, never read back.

That a real minted key returns 200 where the sentinel returned 401, and that the real
Postgres ledger gains a row for one mode and none for the other, is not provable without a
cluster and is deliberately not simulated here — tests-live/test_agent_model_api.py does
it against the live gateway and the live database.
"""

import re
from pathlib import Path

import pytest
import yaml

import agent_provision_harness as harness

REPO = Path(__file__).resolve().parent.parent
TEMPLATE = REPO / "deploy/k8s/64-agent.template.yaml"
SCRIPT = REPO / "deploy/bin/provision-agent.sh"

USER = "baron"
NAME = "probe"
OBJ = f"agent-{USER}-{NAME}"

# The -055 sentinel, spelled out rather than read from the script: this test is what says
# the sentinel is supposed to be gone, and reading the value from the file under test
# would make it agree with a change that removed it.
SENTINEL = "unset-pending-enterpriseaiframework-39d"

# A distinctive stand-in for a user's own provider credential. It must never appear in
# anything a human or a log collector sees.
BYO_KEY = "sk-user-own-anthropic-key-DO-NOT-LEAK-4242"
BYO_BASE = "https://api.anthropic-example.invalid/v1"


@pytest.fixture()
def byo_key_file(tmp_path) -> Path:
    p = tmp_path / "byo.key"
    p.write_text(BYO_KEY + "\n")   # with the trailing newline a real operator's file has
    return p


def _env(container: dict) -> dict:
    out = {}
    for e in container["env"]:
        if "value" in e:
            out[e["name"]] = e["value"]
        else:
            ref = e["valueFrom"]["secretKeyRef"]
            out[e["name"]] = f"secret:{ref['name']}/{ref['key']}"
    return out


# ---------------------------------------------------------------- integrated

@pytest.fixture(scope="module")
def integrated(tmp_path_factory):
    """First provision of an integrated agent: nothing in the cluster yet."""
    return harness.provision(tmp_path_factory.mktemp("integrated"), USER, NAME)


def test_integrated_provisioning_succeeds(integrated):
    assert integrated.returncode == 0, integrated.output


def test_integrated_mints_through_the_control_plane_not_the_gateway(integrated):
    """The mint path, not merely the fact that a key appeared.

    Minting straight at the gateway leaves the ledger's recorded token hash pointing at a
    key that no longer exists, and every later budget change then fails silently against
    it — the exact failure issuance.py's five steps exist to prevent.
    """
    assert "/admin/keys/issue" in integrated.calls, integrated.calls
    assert "/key/generate" not in integrated.calls, (
        "the provisioner talked to the gateway's key API directly:\n" + integrated.calls
    )


def test_integrated_asks_for_contract_ones_surface(integrated):
    """`agents/<name>`, one `::` in the resulting alias. See gateway.AGENT_SURFACE."""
    bodies = (integrated.stub_dir / "curl-bodies.txt").read_text()
    assert f'"surface": "agents/{NAME}"' in bodies, bodies


def test_integrated_replaces_the_sentinel_with_the_real_key(integrated):
    """The 401 that -055 shipped on purpose is what this item closes."""
    stored = integrated.secret_data(f"{OBJ}-key", "OPENAI_API_KEY")
    assert stored == harness.ISSUED_KEY, stored
    assert stored != SENTINEL
    assert SENTINEL not in integrated.applied


def test_integrated_points_the_pod_at_our_gateway_with_that_key(integrated):
    env = _env(integrated.deployment()["spec"]["template"]["spec"]["containers"][0])
    assert env["OPENAI_API_BASE"] == "http://gateway:4000/v1"
    assert env["OPENAI_BASE_URL"] == "http://gateway:4000/v1"
    assert env["OPENAI_API_KEY"] == f"secret:{OBJ}-key/OPENAI_API_KEY"


def test_integrated_is_labelled_as_integrated(integrated):
    dep = integrated.deployment()
    labels = dep["spec"]["template"]["metadata"]["labels"]
    assert labels["agent.enterprise-ai/model-source"] == "integrated"
    assert dep["metadata"]["labels"]["agent.enterprise-ai/model-source"] == "integrated"


def test_the_model_source_label_is_not_in_the_immutable_selector(integrated):
    """A Deployment's selector cannot be changed after creation.

    If `model-source` were in it, switching an agent between integrated and BYO would
    require deleting and recreating the Deployment — which, with a ReadWriteOnce PVC and a
    resident session, is the one operation this surface is built to avoid.
    """
    selector = integrated.deployment()["spec"]["selector"]["matchLabels"]
    assert "agent.enterprise-ai/model-source" not in selector, selector


def _keysum(value: str) -> str:
    import hashlib
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def test_replacing_the_sentinel_rolls_the_pod(integrated):
    """Otherwise the new key never reaches the running agent.

    Env from a `secretKeyRef` is injected when the pod starts and is never updated
    afterwards. An agent upgraded from -055's sentinel whose pod did not restart would go
    on presenting the sentinel and 401ing — with a correct key sitting in its Secret, which
    is the worst possible place for the evidence to be.
    """
    annotations = integrated.deployment()["spec"]["template"]["metadata"]["annotations"]
    assert annotations["checksum/api-key"] == _keysum(harness.ISSUED_KEY)
    assert annotations["checksum/api-key"] != _keysum(SENTINEL)


def test_the_pod_annotation_is_a_hash_and_never_the_key(integrated, byo):
    for run in (integrated, byo):
        rendered = run.applied
        assert harness.ISSUED_KEY not in rendered.split("checksum/api-key")[-1][:200]
    byo_annotations = byo.deployment()["spec"]["template"]["metadata"]["annotations"]
    assert byo_annotations["checksum/api-key"] == _keysum(BYO_KEY)
    assert BYO_KEY not in str(byo_annotations)


def test_reprovisioning_a_healthy_agent_leaves_the_annotation_alone(tmp_path):
    """The other half: a no-op re-run must not roll the pod either.

    Rolling ends the resident session, which is the product. So the annotation tracks the
    credential's identity, not the fact that the provisioner ran.
    """
    live = "sk-already-live-key-1234"
    first = harness.provision(tmp_path / "a", USER, NAME, existing_key=live)
    second = harness.provision(tmp_path / "b", USER, NAME, existing_key=live)
    a = first.deployment()["spec"]["template"]["metadata"]["annotations"]
    b = second.deployment()["spec"]["template"]["metadata"]["annotations"]
    assert a == b == {**a, "checksum/api-key": _keysum(live)}


def test_reprovisioning_an_integrated_agent_does_not_rotate_its_key(tmp_path):
    """Non-disruptive by design, and this is the failure that makes it matter.

    Rotation deletes the old key at the gateway. The pod template's rollout annotation
    tracks the entrypoint, not the key (64-agent.template.yaml), so the running daemon
    would keep presenting a credential that no longer exists — a resident agent 401ing
    with nothing on screen to say why, which on this surface means the session that IS the
    product is silently dead.
    """
    live = "sk-already-live-key-1234"
    again = harness.provision(tmp_path, USER, NAME, existing_key=live)
    assert again.returncode == 0, again.output
    assert "/admin/keys/issue" not in again.calls, again.calls
    assert again.secret_data(f"{OBJ}-key", "OPENAI_API_KEY") == live


def test_a_sentinel_from_055_is_treated_as_no_key_and_is_replaced(tmp_path):
    """The upgrade path: an agent provisioned by -055 gets a real key on the next run."""
    run = harness.provision(tmp_path, USER, NAME, existing_key=SENTINEL)
    assert run.returncode == 0, run.output
    assert "/admin/keys/issue" in run.calls
    assert run.secret_data(f"{OBJ}-key", "OPENAI_API_KEY") == harness.ISSUED_KEY


def test_the_provisioner_names_the_alias_it_verified(integrated):
    """Contract 1's grammar is checked at the point of use, not assumed."""
    assert f"{USER}::agents/{NAME}" in integrated.output


def test_the_alias_guard_bites_when_the_control_plane_answers_wrongly(tmp_path):
    """Fault injection through the real refusal path.

    The control plane is made to answer with the LOSING grammar `<user>::agents::<name>`
    — the exact thing a regression in gateway.py's alias code would produce. That answer
    is not an error anywhere: the key works, the agent runs, and its spend lands on the
    wrong row of the bill forever, because Python and SQL read a three-field alias
    differently (see control-plane/tests/test_agents_alias.py). Nothing downstream will
    ever notice, so the provisioner has to.
    """
    run = harness.provision(tmp_path, USER, NAME, env={"HARNESS_BAD_ALIAS": "1"})
    assert run.returncode != 0, run.output
    assert f"{USER}::agents/{NAME}" in run.output
    assert "agents::" in run.output
    # And nothing was deployed against the bad key.
    assert "Deployment" not in run.applied, run.applied


# ---------------------------------------------------------------- BYO

@pytest.fixture(scope="module")
def byo(tmp_path_factory):
    d = tmp_path_factory.mktemp("byo")
    key_file = d / "byo.key"
    key_file.write_text(BYO_KEY + "\n")
    return harness.provision(
        d, USER, NAME, "--byo-key-file", str(key_file), "--byo-api-base", BYO_BASE
    )


def test_byo_provisioning_succeeds(byo):
    assert byo.returncode == 0, byo.output


def test_byo_mints_no_virtual_key_at_all(byo):
    """The zero-ledger-rows claim, at its cause.

    A BYO agent produces no gateway ledger row because there is no key and no gateway in
    its path — not because a row was filtered out of a view later.
    """
    assert "/admin/keys/issue" not in byo.calls, byo.calls
    assert "/admin/sync" not in byo.calls, byo.calls
    assert harness.ISSUED_KEY not in byo.applied


def test_byo_points_the_pod_at_the_external_provider(byo):
    env = _env(byo.deployment()["spec"]["template"]["spec"]["containers"][0])
    assert env["OPENAI_API_BASE"] == BYO_BASE
    assert env["OPENAI_BASE_URL"] == BYO_BASE
    assert "gateway:4000" not in env["OPENAI_API_BASE"]
    assert env["OPENAI_API_KEY"] == f"secret:{OBJ}-byo/OPENAI_API_KEY"


def test_byo_stores_the_key_in_its_own_secret_verbatim(byo):
    """Separate from the integrated Secret, and byte-exact.

    The trailing newline an operator's key file carries is stripped — a credential with a
    \\n on the end is a 401 from the provider that reads as "BYO does not work".
    """
    assert byo.secret_data(f"{OBJ}-byo", "OPENAI_API_KEY") == BYO_KEY


def test_the_pods_own_secret_holds_no_spendable_key_under_byo(byo):
    """A real key sitting unused in a Secret is a credential with no owner."""
    assert byo.secret_data(f"{OBJ}-key", "OPENAI_API_KEY") == SENTINEL


def test_byo_is_labelled_byo_so_it_can_never_be_a_silent_zero(byo):
    """Finding 4's leak was an unmetered path that rendered as healthy.

    BYO is allowed precisely because it is declared. This label is the declaration that
    -627's tab and -914's meter read, so the difference is a property of the object and
    not a note in a document.
    """
    dep = byo.deployment()
    assert dep["spec"]["template"]["metadata"]["labels"]["agent.enterprise-ai/model-source"] == "byo"
    assert dep["metadata"]["labels"]["agent.enterprise-ai/model-source"] == "byo"


def test_byo_still_carries_the_resident_attribution_labels(byo):
    """BYO removes the inference row, not the residency row (Contract 3).

    The compute meter keys on these labels and not on a virtual key, so a BYO agent still
    bills for the PVC it reserves and the CPU it burns on our hardware.
    """
    labels = byo.deployment()["spec"]["template"]["metadata"]["labels"]
    assert labels["agent.enterprise-ai/user"] == USER
    assert labels["agent.enterprise-ai/name"] == NAME


# ---------------------------------------------------------------- the secret surface

def test_the_byo_key_is_never_printed(byo):
    """Nothing a human reads, a terminal scrolls, or a log collector scrapes."""
    assert BYO_KEY not in byo.output, byo.output


def test_the_byo_key_is_never_put_in_argv(byo):
    """`--from-literal=K=<secret>` would expose it in `ps` to every process on the host.

    Accepted for a virtual key we minted and can revoke in one call; not accepted for the
    user's own provider credential, which we cannot revoke and which spends their money.
    So it travels to kubectl through a 0600 temp file, and this asserts the value appears
    in NO recorded invocation's arguments.
    """
    assert BYO_KEY not in byo.calls, byo.calls


def test_the_byo_key_reaches_the_cluster_only_inside_its_secret(byo):
    """It has to get there somehow; the point is that this is the only route."""
    import base64
    encoded = base64.b64encode(BYO_KEY.encode()).decode()
    assert byo.applied.count(encoded) == 1, byo.applied
    assert BYO_KEY not in byo.applied  # plaintext nowhere, even in the manifest


def test_the_key_is_read_from_a_file_and_a_bare_flag_is_not_accepted(tmp_path):
    """There is no `--byo-key <value>`, and there must not be — see above."""
    run = harness.provision(tmp_path, USER, NAME, "--byo-key", BYO_KEY)
    assert run.returncode != 0
    assert "unknown argument" in run.output


def test_an_env_supplied_key_works_too_and_still_does_not_leak(tmp_path):
    """For a caller that has the key in memory rather than on disk (an API, later)."""
    run = harness.provision(
        tmp_path, USER, NAME, "--byo-api-base", BYO_BASE,
        env={"AGENT_BYO_API_KEY": BYO_KEY},
    )
    assert run.returncode == 0, run.output
    assert run.secret_data(f"{OBJ}-byo", "OPENAI_API_KEY") == BYO_KEY
    assert BYO_KEY not in run.output
    assert BYO_KEY not in run.calls


# ---------------------------------------------------------------- the refusals

def test_a_byo_key_with_no_base_url_is_refused(tmp_path, byo_key_file):
    """The one combination that must never happen by accident: the user's own provider
    credential presented to OUR gateway, which cannot spend it and must not see it."""
    run = harness.provision(tmp_path, USER, NAME, "--byo-key-file", str(byo_key_file))
    assert run.returncode != 0
    assert "byo-api-base" in run.output
    assert BYO_KEY not in run.output


def test_a_byo_base_pointed_back_at_our_gateway_is_refused(tmp_path, byo_key_file):
    """Otherwise an agent is labelled `byo` while being nothing of the kind."""
    run = harness.provision(
        tmp_path, USER, NAME,
        "--byo-key-file", str(byo_key_file),
        "--byo-api-base", "http://gateway:4000/v1",
    )
    assert run.returncode != 0
    assert "gateway" in run.output


def test_switching_a_live_integrated_agent_to_byo_is_refused(tmp_path, byo_key_file):
    """It would leave a spendable virtual key at the gateway with nothing using it.

    Refusing names the thing to revoke first. Silently proceeding would leave a key
    attributed to a user, chargeable, and invisible because no traffic flows through it —
    which is the shape of an unmetered path nobody knows exists.
    """
    run = harness.provision(
        tmp_path, USER, NAME,
        "--byo-key-file", str(byo_key_file), "--byo-api-base", BYO_BASE,
        existing_key="sk-already-live-key-1234",
    )
    assert run.returncode != 0
    assert f"{USER}::agents/{NAME}" in run.output
    assert BYO_KEY not in run.output


def test_an_agent_name_that_is_not_a_slug_is_refused_before_anything_is_applied(tmp_path):
    """The slug is load-bearing for the alias grammar, not cosmetic: a name carrying
    `/` or `::` re-fields the alias and breaks attribution silently."""
    run = harness.provision(tmp_path, USER, "Bad Name")
    assert run.returncode != 0
    assert run.applied == ""


# ---------------------------------------------------------------- template wiring

def test_every_template_placeholder_is_substituted_by_the_provisioner():
    """The drift this catches: a placeholder added to the manifest and not to the sed.

    Its symptom is a Deployment applied with a literal `__API_BASE__` as its OPENAI_API_BASE
    — which the API server accepts, so the agent comes up and fails only at inference time.
    """
    placeholders = set(re.findall(r"__[A-Z_]+__", TEMPLATE.read_text()))
    substituted = set(re.findall(r"s\|(__[A-Z_]+__)\|", SCRIPT.read_text()))
    assert placeholders == substituted, (
        f"template-only: {sorted(placeholders - substituted)}; "
        f"script-only: {sorted(substituted - placeholders)}"
    )


def test_no_rendered_manifest_carries_an_unsubstituted_placeholder(integrated, byo):
    for run, mode in ((integrated, "integrated"), (byo, "byo")):
        assert not re.search(r"__[A-Z_]+__", run.applied), f"{mode}:\n{run.applied}"


def test_the_template_has_no_hardcoded_gateway_url_left():
    """The gateway address must come from the mode, or BYO silently routes through us."""
    body = TEMPLATE.read_text()
    env_block = body[body.index("OPENAI_API_BASE"):body.index("OPENCODE_MODEL")]
    assert "gateway:4000" not in env_block, env_block


def test_the_rendered_template_is_valid_yaml_in_both_modes(integrated, byo):
    for run in (integrated, byo):
        docs = [d for d in yaml.safe_load_all(run.applied) if d]
        kinds = {d["kind"] for d in docs}
        assert {"Deployment", "Service", "PersistentVolumeClaim", "Secret"} <= kinds, kinds
