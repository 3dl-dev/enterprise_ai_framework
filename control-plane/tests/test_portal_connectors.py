"""Wiring an agent to Slack, Discord or a mailbox — from a browser, by its owner only.

WHAT THIS FILE IS FOR

-627 made create/stop/delete self-serve. The thing that makes a resident agent useful —
the chat workspace it answers in, the mailbox it reads — still needed an operator running
`deploy/bin/provision-agent.sh --slack-config-file` with a kubeconfig. This file covers
the endpoint that closes that gap, and it is a security test for the same reason
`test_portal_agents.py` is: the control plane holds create/patch/delete on Secrets across
the whole namespace (deploy/k8s/39-control-plane-rbac.yaml), Kubernetes cannot narrow that
per user, and the owner check in `app/agents.py` is the entire boundary.

A chat connector is a HIGHER-VALUE target than the stop button that came before it. If
one user could write a bot token into another user's agent, the result is not a stopped
pod — it is a live agent holding somebody else's spendable model key, sitting in the
attacker's own Slack workspace, taking instructions from them. Every refusal below is that
attack.

WHAT IS REAL HERE AND WHAT IS NOT

The FakeCluster from `test_portal_agents.py` is reused rather than re-declared: a real
HTTP server in this process holding a real object store, at a real address the shipped
client is pointed at. Nothing in `app/agents.py` or `app/portal.py` is patched. Identity
arrives the way oauth2-proxy delivers it and goes through the shipped `require_user`, so
no test here can hand the endpoint an identity it did not authenticate. The requests the
fake receives are the requests k3s receives.

The claims a fake cannot support — that the rolled pod really comes up carrying the
Secret, that `agent-slack config` in that pod really reports it configured — are proven
against a real cluster in `tests-live/test_portal_connectors.py`.
"""

import base64
import hashlib
import json
import re
import sys
import types
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# The fake cluster, the ledger stub and the loopback client, imported rather than copied.
# A second FakeCluster would drift from the first one within a release and then the two
# files would be asserting against different cluster behaviour.
from test_portal_agents import (  # noqa: E402
    AUDIT, ROOT, client_as, cluster,  # noqa: F401 - `cluster` is used as a fixture
)

from app import agents, portal  # noqa: E402

REPO = ROOT.parent
SHELL = REPO / "deploy" / "bin" / "provision-agent.sh"
STATIC = ROOT / "app" / "portal_static"

# A credential shaped like the real thing. Not a real one — Slack's own scanner revokes a
# token that appears in a public repository, which is the correct behaviour and a very
# annoying way to find out that a fixture was too realistic.
SLACK = {
    "AGENT_SLACK_BOT_TOKEN": "xoxb-0000-fixture-not-a-real-token",
    "AGENT_SLACK_APP_TOKEN": "xapp-0000-fixture-not-a-real-token",
    "AGENT_SLACK_DEFAULT_CHANNEL": "C0123ABCD",
}
DISCORD = {"AGENT_DISCORD_BOT_TOKEN": "discord-fixture-not-a-real-token"}
EMAIL = {
    "AGENT_EMAIL_ADDRESS": "helper@contoso.example",
    "AGENT_EMAIL_PASSWORD": "fixture-not-a-real-password",
    "AGENT_EMAIL_SMTP_HOST": "smtp.contoso.example",
    "AGENT_EMAIL_IMAP_HOST": "imap.contoso.example",
}


def secret_data(cluster, name: str) -> dict[str, str]:
    """A Secret's contents as the pod would see them, decoded from the store."""
    obj = cluster.get("secrets", name)
    assert obj is not None, f"no Secret {name}; the store holds {cluster.names('secrets')}"
    return {k: base64.b64decode(v).decode() for k, v in (obj.get("data") or {}).items()}


def annotations(cluster, dep_name: str) -> dict[str, str]:
    dep = cluster.get("deployments", dep_name)
    return (((dep.get("spec") or {}).get("template") or {})
            .get("metadata") or {}).get("annotations") or {}


def configure(client, name, kind, values):
    return client.post(f"/portal/api/agents/{name}/connectors",
                       json={"kind": kind, "values": values})


# ---------------------------------------------------------------- one schema, three copies


def _shell_allowlists() -> dict[str, dict]:
    """What `provision-agent.sh` accepts, parsed out of the script itself.

    Not transcribed — parsed. A transcription is a fourth copy that goes stale silently,
    which is the exact failure this test exists to catch.
    """
    text = re.sub(r"\\\n\s*", " ", SHELL.read_text())
    calls = re.findall(
        r'provision_connector\s+(\w+)\s+"[^"]*"\s+"[^"]*"\s+"([^"]*)"\s+(\w+)\s+'
        r'"[^"]*"\s+"([^"]*)"\s+"([^"]*)"',
        text,
    )
    assert calls, "no provision_connector calls found; the shell script's shape changed"
    return {
        label: {
            "noun": noun,
            "sum_key": sum_key,
            "allowed": tuple(allowed.split()),
            "required": tuple(required.split()),
        }
        for label, noun, sum_key, allowed, required in calls
    }


def _shell_agent_files() -> tuple[str, ...]:
    """`AGENT_FILES=(...)` — every file provision-agent.sh puts in the pod's ConfigMap."""
    body = re.search(r"AGENT_FILES=\((.*?)\)", SHELL.read_text(), re.S)
    assert body, "provision-agent.sh no longer declares AGENT_FILES"
    return tuple(body.group(1).split())


def test_the_python_schema_matches_the_shell_provisioners_allowlists():
    """One schema, and the drift between its two implementations is measured.

    The endpoint and `provision-agent.sh` write the SAME Secret, which the same agent
    tools read. A key one accepts and the other does not is either a setting no user can
    reach from the browser, or an environment variable in the agent pod that nothing on
    the operator path ever validated. Neither shows up as a failure anywhere else.
    """
    shell = _shell_allowlists()
    assert set(shell) == set(agents.CONNECTORS), (
        f"the two paths offer different connectors: shell {sorted(shell)}, "
        f"control plane {sorted(agents.CONNECTORS)}"
    )
    for kind, spec in agents.CONNECTORS.items():
        assert spec.allowed == shell[kind]["allowed"], (
            f"{kind}: the allowlists have diverged.\n"
            f"  control plane: {spec.allowed}\n  provision-agent.sh: {shell[kind]['allowed']}"
        )
        assert spec.required == shell[kind]["required"], f"{kind}: required keys diverged"
        assert spec.sum_key == shell[kind]["sum_key"], (
            f"{kind}: the checksum key diverged, so a credential written by one path "
            "would look unconfigured to the other and roll the pod for no reason"
        )


def test_the_browser_form_offers_exactly_the_keys_the_endpoint_accepts():
    """The third copy: the field list `app.js` renders inputs from.

    A field the page invents is refused by the endpoint with an error the user cannot act
    on. A required field the page omits produces a 400 after the agent has already been
    created. Both are caught here rather than by somebody filling the form in.
    """
    js = (STATIC / "app.js").read_text()
    block = js[js.index("const CONNECTOR_FIELDS"):js.index("let SETUP_MODE")]
    for kind, spec in agents.CONNECTORS.items():
        section = block[block.index(f"{kind}: ["):]
        section = section[:section.index("],")]
        offered = tuple(re.findall(r'key:\s*"([A-Z_]+)"', section))
        unknown = [k for k in offered if k not in spec.allowed]
        assert not unknown, f"the {kind} form offers keys the endpoint refuses: {unknown}"
        missing = [k for k in spec.required if k not in offered]
        assert not missing, (
            f"the {kind} form has no input for {missing}, which the endpoint requires — "
            "the user would create an agent and then be told it is incomplete"
        )
        # Every credential-shaped field must be a password input. A bot token in a text
        # field is a token on the screen of a shared machine.
        for key in offered:
            if key.endswith(("_TOKEN", "_PASSWORD")):
                entry = section[section.index(f'key: "{key}"'):]
                entry = entry[:entry.index("}")]
                assert "secret: true" in entry, (
                    f"{key} is rendered as a visible text input"
                )


# ---------------------------------------------------------------- the owner's path


def test_a_user_wires_slack_from_the_browser_and_the_pod_is_rolled(cluster):
    """The whole point of the item, end to end through the shipped endpoint.

    The expected Secret name, its keys and the annotation that rolls the pod are not read
    off this endpoint — they are the ones `deploy/k8s/64-agent.template.yaml` mounts by
    name and `deploy/agent/agent-slack` reads by name. If this endpoint wrote
    `agent-alice-scraper-chat`, or `SLACK_BOT_TOKEN`, every assertion below would still
    pass against a self-consistent implementation and the agent would come up with no
    Slack at all.
    """
    cluster.add_agent("alice", "scraper")
    before = json.dumps(cluster.get("deployments", "agent-alice-scraper"))

    resp = configure(client_as("alice"), "scraper", "slack", SLACK)
    assert resp.status_code == 200, resp.text

    # The name the template mounts, spelled out rather than derived from the code.
    stored = secret_data(cluster, "agent-alice-scraper-slack")
    assert stored["AGENT_SLACK_BOT_TOKEN"] == SLACK["AGENT_SLACK_BOT_TOKEN"]
    assert stored["AGENT_SLACK_APP_TOKEN"] == SLACK["AGENT_SLACK_APP_TOKEN"]
    assert stored["AGENT_SLACK_DEFAULT_CHANNEL"] == "C0123ABCD"
    assert set(stored) == set(SLACK) | {"AGENT_SLACK_CONFIG_SUM"}, (
        "the Secret carries something other than the supplied settings and the checksum "
        "the shell path stores beside them — every key here becomes an environment "
        "variable in a pod holding a spendable model key"
    )

    # The roll. `envFrom` is injected at pod start and never updated, so a credential
    # stored without this reaches a running agent never.
    annos = annotations(cluster, "agent-alice-scraper")
    assert annos["checksum/slack"] == stored["AGENT_SLACK_CONFIG_SUM"], (
        "the pod-template annotation does not match the checksum stored beside the "
        "credential; a later re-render would roll the agent for a credential that did "
        "not change"
    )
    assert annos["checksum/slack"] not in ("", "none"), "the pod was not rolled"
    assert SLACK["AGENT_SLACK_BOT_TOKEN"] not in json.dumps(annos), (
        "the annotation carries the credential rather than a hash of it — annotations are "
        "world-readable to anything that can read the Deployment"
    )

    # And nothing else about the agent moved. A patch that replaced the pod template
    # would take the containers, the image and the volumes with it.
    dep = cluster.get("deployments", "agent-alice-scraper")
    assert json.loads(before)["spec"]["replicas"] == dep["spec"]["replicas"]
    assert dep["metadata"]["labels"]["agent.enterprise-ai/user"] == "alice"

    assert ("alice", "agent.connector.configure", "alice/scraper") in AUDIT


def test_the_credential_is_never_returned_by_anything(cluster):
    """Set-once, Contract 4. There is no read path, and the write path does not echo.

    An endpoint that returned what it stored would make every later XSS or confused
    deputy on this origin a credential exfiltration, and would put a live bot token in
    whatever logs the response body.
    """
    cluster.add_agent("alice", "scraper")
    alice = client_as("alice")
    resp = configure(alice, "scraper", "slack", SLACK)
    body = resp.text
    for value in SLACK.values():
        assert value not in body, f"the response echoed {value!r} back"
    assert resp.json()["keys"] == sorted(SLACK), (
        "the response should name the keys that were set — that is what tells a user the "
        "channel they typed was accepted — and nothing else"
    )

    listed = alice.get("/portal/api/agents").text
    for value in SLACK.values():
        assert value not in listed, "the agent listing carries the credential"
    row = alice.get("/portal/api/agents").json()["agents"][0]
    assert row["connectors"] == {"slack": True, "discord": False, "email": False}


def test_email_and_discord_write_their_own_secret_and_their_own_annotation(cluster):
    """Three Secrets, three annotations. Rotating one must not roll the pod for the others.

    They have different lifecycles — a Slack token is rotated in Slack's admin UI, a mail
    password in the provider's — and one combined Secret would mean re-supplying all of
    them to change any of them.
    """
    cluster.add_agent("alice", "scraper")
    alice = client_as("alice")
    assert configure(alice, "scraper", "slack", SLACK).status_code == 200
    slack_sum = annotations(cluster, "agent-alice-scraper")["checksum/slack"]

    assert configure(alice, "scraper", "discord", DISCORD).status_code == 200
    assert configure(alice, "scraper", "email", EMAIL).status_code == 200

    assert secret_data(cluster, "agent-alice-scraper-discord")["AGENT_DISCORD_BOT_TOKEN"] \
        == DISCORD["AGENT_DISCORD_BOT_TOKEN"]
    assert secret_data(cluster, "agent-alice-scraper-email")["AGENT_EMAIL_ADDRESS"] \
        == EMAIL["AGENT_EMAIL_ADDRESS"]

    annos = annotations(cluster, "agent-alice-scraper")
    assert annos["checksum/slack"] == slack_sum, (
        "configuring Discord changed the Slack checksum; every connector change would "
        "then read as a Slack rotation in `kubectl describe`"
    )
    assert len({annos["checksum/slack"], annos["checksum/discord"],
                annos["checksum/email"]}) == 3

    row = alice.get("/portal/api/agents").json()["agents"][0]
    assert row["connectors"] == {"slack": True, "discord": True, "email": True}


def test_resupplying_the_same_credential_does_not_roll_a_healthy_agent(cluster):
    """Restarting an agent ends the resident session that is the entire product.

    So the checksum is over the credential's canonical form, not over the request: the
    same tokens re-submitted — in a different field order, with the trailing space a paste
    leaves behind — must produce the SAME annotation.
    """
    cluster.add_agent("alice", "scraper")
    alice = client_as("alice")
    configure(alice, "scraper", "slack", SLACK)
    first = annotations(cluster, "agent-alice-scraper")["checksum/slack"]

    reordered = {k: (v + " ") for k, v in reversed(list(SLACK.items()))}
    configure(alice, "scraper", "slack", reordered)
    assert annotations(cluster, "agent-alice-scraper")["checksum/slack"] == first, (
        "the same credential produced a different checksum and would restart a healthy "
        "agent, ending its session, for no change"
    )

    rotated = {**SLACK, "AGENT_SLACK_BOT_TOKEN": "xoxb-0000-rotated"}
    configure(alice, "scraper", "slack", rotated)
    assert annotations(cluster, "agent-alice-scraper")["checksum/slack"] != first, (
        "a ROTATED token did not roll the pod — the agent would keep presenting the old "
        "one and fail as a silent invalid_auth"
    )


def test_a_created_agent_starts_with_no_connector_and_says_so(cluster):
    """The placeholders -a4e and -783 added to the template must be substituted.

    They were not: -627's renderer predates them, so an agent created from the portal
    carried the literal `checksum/slack: "__SLACKSUM__"`. Harmless as an annotation value
    and therefore invisible — but it is the same class of defect as a forgotten `__USER__`,
    and the connector state this page renders is read from exactly these annotations.
    """
    cluster.add_workspace_pod()
    assert client_as("alice").post(
        "/portal/api/agents", json={"name": "helper"}).status_code == 201
    annos = annotations(cluster, "agent-alice-helper")
    assert annos["checksum/email"] == "none"
    assert annos["checksum/slack"] == "none"
    assert annos["checksum/discord"] == "none"
    row = client_as("alice").get("/portal/api/agents").json()["agents"][0]
    assert row["connectors"] == {"slack": False, "discord": False, "email": False}


def test_a_created_agent_gets_every_tool_its_connectors_need(cluster):
    """The credential is worthless if the program that reads it is not in the pod.

    `-627`'s create shipped `entrypoint.sh` alone into the DEPLOYMENT-WIDE
    `agent-entrypoint` ConfigMap, with a server-side apply carrying `force=true` — so
    pressing Create did not merely make an agent without chat tools, it took
    `agent-slack`, `agent-email`, `agent-discord` and `agentws.py` away from every agent
    an operator had already wired. Found by running the connector endpoint against live
    k3s: the Secret and the roll were both correct and `/etc/agent/agent-slack config`
    exited 1, because the file was not there.

    The checksum is asserted against the value `provision-agent.sh` computes — the hash of
    the same eight files concatenated in the same order, recomputed here from the
    repository — because two different values from one repository means provisioning by
    either route restarts every agent created by the other, ending resident sessions.
    """
    cluster.add_workspace_pod()
    # The `agent-entrypoint` tooling ConfigMap belongs to the opencode render (the shell
    # tools its entrypoint puts on PATH). A hermes agent handles connectors natively and
    # ships no such ConfigMap, so this Code-pillar assertion targets the interim opencode
    # path explicitly (type=openclaw, pending enterpriseaiframework-ff7).
    assert client_as("alice").post(
        "/portal/api/agents", json={"name": "helper", "type": "openclaw"}).status_code == 201

    shipped = cluster.get("configmaps", "agent-entrypoint")["data"]
    agent_dir = REPO / "deploy" / "agent"
    expected = _shell_agent_files()
    assert set(shipped) == set(expected), (
        f"the portal ships {sorted(shipped)} but provision-agent.sh ships "
        f"{sorted(expected)}; the ConfigMap is shared and written with force=true, so the "
        "shorter list deletes the difference from every agent in the namespace"
    )
    for name in expected:
        assert shipped[name] == (agent_dir / name).read_text(), f"{name} was altered"

    concatenated = b"".join((agent_dir / name).read_bytes() for name in expected)
    dep = cluster.get("deployments", "agent-alice-helper")
    annos = dep["spec"]["template"]["metadata"]["annotations"]
    assert annos["checksum/entrypoint"] == \
        hashlib.sha256(concatenated).hexdigest()[:16], (
        "the portal's entrypoint checksum is not the one provision-agent.sh computes over "
        "the same files, so the two paths would roll each other's agents"
    )


def test_deleting_an_agent_takes_its_connector_credentials_with_it(cluster):
    """A left-behind Secret is a live bot token with nothing using it.

    The same argument that makes delete revoke the virtual key: the tenant's own Slack
    credential, with whatever scopes they granted it, must not survive the agent it was
    written for. Before this endpoint existed nothing could create one from the portal, so
    nothing had noticed the omission.
    """
    cluster.add_agent("alice", "scraper")
    alice = client_as("alice")
    configure(alice, "scraper", "slack", SLACK)
    configure(alice, "scraper", "email", EMAIL)
    assert "agent-alice-scraper-slack" in cluster.names("secrets")

    alice.request("DELETE", "/portal/api/agents/scraper")
    assert cluster.names("secrets") == [], (
        f"a credential survived the delete: {cluster.names('secrets')}"
    )


# ---------------------------------------------------------------- the refusals


@pytest.mark.parametrize("kind", ["slack", "discord", "email"])
def test_a_second_user_cannot_wire_another_users_agent(cluster, kind):
    """THE attack. 404, and no Secret, and no roll.

    Succeeding here would not be a defaced page: it would put a live agent holding
    alice's spendable model key into mallory's Slack workspace, reading mallory's
    instructions, on alice's bill.

    404 rather than 403 for the reason the rest of this surface uses: a distinct 403
    confirms to a prober that an agent by that name exists and belongs to somebody.
    """
    cluster.add_agent("alice", "scraper")
    values = {"slack": SLACK, "discord": DISCORD, "email": EMAIL}[kind]
    resp = configure(client_as("mallory"), "scraper", kind, values)
    assert resp.status_code == 404, (
        f"mallory configured {kind} on alice's agent ({resp.status_code}): {resp.text}"
    )
    assert cluster.get("secrets", f"agent-alice-scraper-{kind}") is None
    assert f"checksum/{kind}" not in annotations(cluster, "agent-alice-scraper")


def test_a_hyphen_collision_cannot_wire_another_users_agent(cluster):
    """Name derivation alone is not enough, and this is where it costs the most.

        user "alice-bot" + agent "two"     -> agent-alice-bot-two
        user "alice"     + agent "bot-two" -> agent-alice-bot-two

    Both identities derive the same object name, so only re-reading the object and
    checking its owner LABEL closes it. Without that, alice writes her own bot token into
    alice-bot's running agent and it joins her workspace.
    """
    cluster.add_agent("alice-bot", "two")
    resp = configure(client_as("alice"), "bot-two", "slack", SLACK)
    assert resp.status_code == 404, resp.text
    assert cluster.get("secrets", "agent-alice-bot-two-slack") is None
    assert "checksum/slack" not in annotations(cluster, "agent-alice-bot-two")


def test_configuring_an_agent_that_does_not_exist_creates_nothing(cluster):
    """No Secret without a Deployment to own it.

    Writing one first would leave an orphan credential in the namespace addressed by a
    name anybody can choose — and the next person to create an agent with that name would
    inherit it.
    """
    resp = configure(client_as("alice"), "ghost", "slack", SLACK)
    assert resp.status_code == 404, resp.text
    assert cluster.names("secrets") == []


@pytest.mark.parametrize("key", [
    "PATH",                     # arbitrary code execution dressed up as a chat setting
    "LD_PRELOAD",
    "OPENAI_API_KEY",           # the agent's spendable key
    "OPENCODE_SERVER_PASSWORD",  # the console credential
    "AGENT_SLACK_CONFIG_SUM",   # suppress the roll, leave the pod on the old credential
    "AGENT_EMAIL_PASSWORD",     # a real key, but not one this connector owns
    "agent_slack_bot_token",    # the allowlist is exact, not case-insensitive
])
def test_a_key_outside_the_connectors_allowlist_is_refused(cluster, key):
    """The allowlist is the security control, and it refuses rather than drops.

    This Secret is injected with `envFrom`, so every key in it becomes an environment
    variable in a container that runs a coding agent and holds a spendable API key. The
    template's explicit `env:` wins for the two names somebody thought of; the allowlist
    is what covers the rest. A dropped key would be worse than a refused one — the user
    would believe they had supplied it.
    """
    cluster.add_agent("alice", "scraper")
    resp = configure(client_as("alice"), "scraper", "slack", {**SLACK, key: "x"})
    assert resp.status_code == 400, f"{key} was accepted ({resp.status_code})"
    assert cluster.get("secrets", "agent-alice-scraper-slack") is None, (
        "the Secret was written despite the refusal"
    )


@pytest.mark.parametrize("value", [
    "xoxb-good\nAGENT_SLACK_APP_TOKEN=smuggled",  # a second setting inside one value
    "xoxb\r\nPATH=/tmp/evil",
    "xoxb-\rgood",                                # a bare CR in the middle
    "xoxb\x00truncated",
    "xoxb\x1b[2Jclear",                           # a terminal escape into anything reading it
])
def test_a_value_carrying_a_line_break_or_control_character_is_refused(cluster, value):
    """The injection refusal, and the paste that silently breaks an agent.

    `provision-agent.sh` refuses a whole CRLF file with a diagnosis, because `token\\r`
    fails as a 401 nobody connects back to the file they saved on Windows. A browser field
    produces the same thing from a copied line, and an interior newline in a value that is
    later read as KEY=value lines is a way to smuggle a second key past the allowlist.
    """
    cluster.add_agent("alice", "scraper")
    resp = configure(client_as("alice"), "scraper", "slack",
                     {**SLACK, "AGENT_SLACK_BOT_TOKEN": value})
    assert resp.status_code == 400, f"{value!r} was accepted ({resp.status_code})"
    assert cluster.get("secrets", "agent-alice-scraper-slack") is None


def test_the_whitespace_a_paste_leaves_behind_is_trimmed_not_stored(cluster):
    """The one place this path deliberately differs from the config-file path.

    A file's values are taken literally, quotes and all, because a file is authored. A
    token pasted into an HTML field routinely arrives with a trailing space or newline
    from the copy, no credential in these three schemas has meaningful surrounding
    whitespace, and storing `xoxb-… ` produces a 401 that points at nothing. Trimmed at
    the EDGES — including the carriage return a Windows copy leaves behind, which is the
    case the shell path can only refuse because it is handed a whole file. Anything left
    INSIDE the value is refused by the test above, not trimmed.
    """
    cluster.add_agent("alice", "scraper")
    padded = {k: f"  {v}\t\r\n" for k, v in SLACK.items()}
    assert configure(client_as("alice"), "scraper", "slack", padded).status_code == 200
    stored = secret_data(cluster, "agent-alice-scraper-slack")
    assert stored["AGENT_SLACK_BOT_TOKEN"] == SLACK["AGENT_SLACK_BOT_TOKEN"]
    assert stored["AGENT_SLACK_APP_TOKEN"] == SLACK["AGENT_SLACK_APP_TOKEN"]


@pytest.mark.parametrize("kind,values,missing", [
    ("slack", {"AGENT_SLACK_BOT_TOKEN": "xoxb-x"}, "AGENT_SLACK_APP_TOKEN"),
    ("slack", {"AGENT_SLACK_APP_TOKEN": "xapp-x"}, "AGENT_SLACK_BOT_TOKEN"),
    ("slack", {**SLACK, "AGENT_SLACK_BOT_TOKEN": "   "}, "AGENT_SLACK_BOT_TOKEN"),
    ("discord", {"AGENT_DISCORD_DEFAULT_CHANNEL": "1"}, "AGENT_DISCORD_BOT_TOKEN"),
    ("email", {k: v for k, v in EMAIL.items() if k != "AGENT_EMAIL_IMAP_HOST"},
     "AGENT_EMAIL_IMAP_HOST"),
])
def test_a_half_configured_connector_is_refused_rather_than_stored(cluster, kind, values,
                                                                   missing):
    """Every required key, or none of them.

    Slack with a bot token and no app token can post and can never hear a reply. A mailbox
    with SMTP and no IMAP can send and can never read. Both present as "the agent ignores
    me" long after the configuration that caused it, with nothing connecting the two.
    """
    cluster.add_agent("alice", "scraper")
    resp = configure(client_as("alice"), "scraper", kind, values)
    assert resp.status_code == 400, resp.text
    assert missing in resp.json()["detail"], (
        "the refusal must name the missing setting; an error that does not is a form "
        "somebody fills in twice"
    )
    assert cluster.get("secrets", f"agent-alice-scraper-{kind}") is None


@pytest.mark.parametrize("kind", ["", "teams", "Slack", "../slack", None, ["slack"]])
def test_an_unknown_connector_is_refused(cluster, kind):
    cluster.add_agent("alice", "scraper")
    resp = client_as("alice").post("/portal/api/agents/scraper/connectors",
                                   json={"kind": kind, "values": SLACK})
    assert resp.status_code == 400, f"{kind!r} was accepted ({resp.status_code})"
    assert cluster.names("secrets") == ["agent-alice-scraper-key"]


def test_a_value_that_is_not_a_string_is_refused(cluster):
    """JSON can carry a list or an object; a Secret cannot, and a coerced one is a lie."""
    cluster.add_agent("alice", "scraper")
    resp = configure(client_as("alice"), "scraper", "slack",
                     {**SLACK, "AGENT_SLACK_DEFAULT_CHANNEL": ["C1", "C2"]})
    assert resp.status_code == 400, resp.text


def test_an_overlong_value_is_refused(cluster):
    """An unbounded string from a request body is a pod that cannot start, from a browser."""
    cluster.add_agent("alice", "scraper")
    resp = configure(client_as("alice"), "scraper", "slack",
                     {**SLACK, "AGENT_SLACK_BOT_TOKEN": "x" * 9000})
    assert resp.status_code == 400, resp.text


def test_a_forged_identity_header_from_off_pod_reaches_no_connector(cluster):
    """`require_user` guards this endpoint too.

    Any workload in the namespace can reach this Service and set any header it likes. This
    endpoint writes a Secret and rolls a pod, so an endpoint that forgot the dependency
    would let one do it to anybody's agent.
    """
    import starlette.requests

    cluster.add_agent("alice", "scraper")
    api = FastAPI()
    api.include_router(portal.router)
    c = TestClient(api, client=("127.0.0.1", 41000), raise_server_exceptions=False)
    original = starlette.requests.Request.client.fget
    starlette.requests.Request.client = property(
        lambda self: types.SimpleNamespace(host="10.42.0.99", port=1))
    try:
        resp = c.post("/portal/api/agents/scraper/connectors",
                      headers={"X-Auth-Request-Preferred-Username": "alice"},
                      json={"kind": "slack", "values": SLACK})
        assert resp.status_code == 403, (
            f"a forged identity header from off-pod configured an agent ({resp.status_code})"
        )
    finally:
        starlette.requests.Request.client = property(original)
    assert cluster.get("secrets", "agent-alice-scraper-slack") is None


def test_a_missing_rbac_verb_on_secrets_is_reported_as_a_deployment_fault(cluster):
    """A 403 from the API server must not reach a user as a blank 500.

    It means deploy/k8s/39-control-plane-rbac.yaml was not applied — which already grants
    secrets create/patch, so this is a deployment left stale, and the fix is one line.
    """
    cluster.add_agent("alice", "scraper")
    cluster.deny.add("patch:secrets")
    resp = configure(client_as("alice"), "scraper", "slack", SLACK)
    assert resp.status_code == 500
    assert "39-control-plane-rbac.yaml" in resp.json()["detail"], resp.text


# ---------------------------------------------------------------- the surface around it


def test_the_flat_body_shape_the_wizard_may_send_is_accepted(cluster):
    """`{kind, AGENT_...}` as well as `{kind, values: {...}}`.

    Both are documented on the endpoint. The keys are checked against the allowlist
    either way, so the flat form widens the accepted SHAPE and not the accepted KEYS.
    """
    cluster.add_agent("alice", "scraper")
    resp = client_as("alice").post("/portal/api/agents/scraper/connectors",
                                   json={"kind": "slack", **SLACK})
    assert resp.status_code == 200, resp.text
    assert secret_data(cluster, "agent-alice-scraper-slack")["AGENT_SLACK_BOT_TOKEN"] \
        == SLACK["AGENT_SLACK_BOT_TOKEN"]


def test_the_wizard_creates_through_the_627_endpoint_and_then_configures():
    """The page's contract with the two endpoints, asserted rather than described.

    The wizard must not grow a second create path: -627's `POST /portal/api/agents` is
    what mints the virtual key through `issuance.issue` and applies the template, and a
    shortcut around it would produce an agent with no key and no ledger row.
    """
    js = (STATIC / "app.js").read_text()
    form = js[js.index('$("setup-form").addEventListener'):]
    form = form[:form.index("$(\"agent-new\").addEventListener")]
    assert 'post("/portal/api/agents",' in form, (
        "the wizard does not create through the -627 endpoint"
    )
    assert "configureConnector(name, w.kind, w.values)" in form
    assert '`/portal/api/agents/${encodeURIComponent(name)}/connectors`' in js
    # Create first, then configure. The reverse order writes a Secret for a Deployment
    # that does not exist yet, which the endpoint refuses.
    assert form.index('post("/portal/api/agents",') < form.index("configureConnector("), (
        "the wizard configures before it creates"
    )


def test_the_wizard_defaults_to_slack_and_offers_discord_and_neither():
    """Slack is the default chat platform, per the item, and it is a real choice.

    A selector with no `none` would make a chat connector mandatory to create an agent
    from this dialog, which is not what -627 promises.
    """
    index = (STATIC / "index.html").read_text()
    chat = index[index.index('id="setup-chat"'):]
    chat = chat[:chat.index("</select>")]
    assert '<option value="slack" selected>' in chat, "Slack is not the default"
    assert 'value="discord"' in chat and 'value="none"' in chat


def test_nothing_on_the_page_populates_a_credential_input_from_a_response():
    """Write-only inputs, checked mechanically.

    There is no endpoint that returns a credential, so this cannot regress by accident —
    but a future `value` assignment from a fetched object is exactly how a set-once field
    becomes a readable one, and it would be one line.
    """
    js = (STATIC / "app.js").read_text()
    setup = js[js.index("const CONNECTOR_FIELDS"):js.index('$("agent-new").addEventListener')]
    assignments = re.findall(r"\.value\s*=\s*([^;\n]+)", setup)
    for rhs in assignments:
        assert ('""' in rhs or '"slack"' in rhs or "$(" in rhs), (
            f"a credential input is assigned from {rhs.strip()!r}; inputs in this dialog "
            "are write-only and must only ever be cleared"
        )
    assert 'input.type = f.secret ? "password" : "text"' in setup


def test_the_chat_and_code_tabs_are_untouched_by_the_wizard():
    """Additive, and asserted. The camp runs on the first two tabs.

    `test_portal_agents.py` makes the same assertion for -627; it is repeated here because
    this change edits the same two files and a regression would land in exactly the same
    place.
    """
    index = (STATIC / "index.html").read_text()
    js = (STATIC / "app.js").read_text()
    for marker in ('id="tab-chat"', 'id="tab-code"', 'id="view-chat"', 'id="view-code"',
                   'id="frame-chat"', 'id="frame-code"', 'id="code-fallback"'):
        assert marker in index, f"the setup wizard removed {marker} from the portal page"
    assert 'frame.src = which === "code" ? "/workshop/" : (LINKS.chat || "/");' in js
    # The one-click create the camp uses is still one click.
    assert 'id="agent-create"' in index and 'id="agent-new"' in index
