"""The agent's boot has to hand opencode its chat tools, and only the ones it can use.

Three claims, and the last two are the ones that keep the surface alive:

  1. When the agent has Slack or Discord configured, `agent-slack` / `agent-discord` are on
     PATH and opencode is told the tool exists — by COMPOSING the image's own opencode
     config with extra `instructions` entries. The image cannot be edited (Contract 6
     freezes deploy/workspace/, including the Dockerfile that bakes that config), so
     composition at boot is the only channel.
  2. Only CONFIGURED tools are advertised. An agent with Slack and no mailbox must not be
     handed EMAIL.md: instructions for a tool whose credential is absent are instructions to
     attempt something that will fail, and the model cannot tell the difference in advance.
  3. When that composition fails for ANY reason, the agent still boots on the image's
     config. opencode 1.18.7 hard-errors and exits non-zero on a config it cannot parse, so
     a bad render would turn a missing documentation file into an agent that never starts —
     trading the entire resident surface for a markdown file.

The real deploy/agent/entrypoint.sh is executed. `opencode` is a recorder, because the
question is what the entrypoint hands it, and the pod's absolute paths are supplied through
the same environment variables the pod template sets.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
ENTRYPOINT = REPO / "deploy/agent/entrypoint.sh"

IMAGE_CONFIG = {
    "$schema": "https://opencode.ai/config.json",
    "provider": {"enterprise-ai": {"npm": "@ai-sdk/openai-compatible",
                                   "options": {"baseURL": "{env:OPENAI_API_BASE}"}}},
    "model": "enterprise-ai/glm-5.2@deepinfra",
    "instructions": ["/etc/opencode/PLATFORM.md", "/etc/opencode/tenant/TENANT.md"],
    "mcp": {"echo": {"type": "remote", "url": "http://mcp-echo:8080/mcp", "enabled": True}},
}

OPENCODE_STUB = """#!/usr/bin/env bash
python3 - "$@" <<'PY' >> "$RECORD"
import json, os, sys
print(json.dumps({"argv": sys.argv[1:],
                  "OPENCODE_CONFIG": os.environ.get("OPENCODE_CONFIG"),
                  "PATH": os.environ.get("PATH")}))
PY
if [[ "${1:-}" == "debug" && -n "${STUB_REJECT:-}" ]]; then
    echo "config error: unrecognised key" >&2
    exit 1
fi
exit 0
"""

# The one variable per connector that the entrypoint keys off. Each arrives from that
# connector's per-agent Secret through the pod template's `envFrom`, so "has Slack" is
# decided by the cluster and never by a flag in this file.
TRIGGERS = {
    "email": {"AGENT_EMAIL_SMTP_HOST": "smtp.office365.com"},
    "slack": {"AGENT_SLACK_BOT_TOKEN": "xoxb-not-a-real-token"},
    "discord": {"AGENT_DISCORD_BOT_TOKEN": "not-a-real-discord-token"},
}
DOCS = {"email": "/etc/agent/EMAIL.md",
        "slack": "/etc/agent/SLACK.md",
        "discord": "/etc/agent/DISCORD.md"}


def _boot(tmp_path: Path, *, connectors=(), image_config=IMAGE_CONFIG,
          reject_config=False) -> dict:
    """Run the real entrypoint to the point where it would exec the daemon."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    record = tmp_path / "opencode-invocations.jsonl"
    stub = bin_dir / "opencode"
    stub.write_text(OPENCODE_STUB)
    stub.chmod(0o755)

    config_path = tmp_path / "image-opencode.json"
    config_path.write_text(image_config if isinstance(image_config, str)
                           else json.dumps(image_config))

    env = {
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        "HOME": str(tmp_path / "home"),
        "RECORD": str(record),
        "AGENT_USER": "baron",
        "AGENT_NAME": "chatter",
        "OPENCODE_SERVER_PASSWORD": "not-a-real-password",
        "AGENT_WORKDIR": str(tmp_path / "work"),
        "XDG_DATA_HOME": str(tmp_path / "state"),
        "OPENCODE_CONFIG": str(config_path),
    }
    (tmp_path / "home").mkdir()
    if reject_config:
        env["STUB_REJECT"] = "1"
    for connector in connectors:
        env.update(TRIGGERS[connector])

    proc = subprocess.run(["bash", str(ENTRYPOINT)], capture_output=True, text=True,
                          timeout=180, env=env, cwd=str(tmp_path))
    assert proc.returncode == 0, f"the entrypoint did not reach the daemon:\n{proc.stderr}"
    assert record.exists(), f"the entrypoint never ran opencode:\n{proc.stdout}{proc.stderr}"
    calls = [json.loads(line) for line in record.read_text().splitlines() if line.strip()]
    served = [c for c in calls if c["argv"][:1] == ["serve"]]
    assert served, f"the entrypoint never exec'd the daemon; calls were {calls}"
    return {**served[-1], "calls": calls, "stdout": proc.stdout, "stderr": proc.stderr,
            "state_dir": tmp_path / "state", "image_config": str(config_path)}


def _instructions(booted) -> list:
    return json.loads(Path(booted["OPENCODE_CONFIG"]).read_text())["instructions"]


@pytest.mark.parametrize("connector", ["slack", "discord"])
def test_a_configured_chat_connector_is_on_path_and_opencode_is_told_about_it(tmp_path,
                                                                              connector):
    booted = _boot(tmp_path, connectors=[connector])

    assert "/etc/agent" in booted["PATH"].split(":"), (
        f"the tool directory is not on PATH, so `agent-{connector}` is 'command not found' "
        "to opencode's shell tool."
    )
    composed = Path(booted["OPENCODE_CONFIG"])
    assert str(composed) != booted["image_config"], \
        "opencode was pointed at the image config, so it was never told the tool exists"

    config = json.loads(composed.read_text())
    assert config["instructions"][-1] == DOCS[connector]
    # Everything else carried over from the image config. A composition that rebuilt the
    # file instead of extending it would silently fork the provider block and the model
    # catalogue the moment either changed in the image.
    assert config["provider"] == IMAGE_CONFIG["provider"]
    assert config["model"] == IMAGE_CONFIG["model"]
    assert config["mcp"] == IMAGE_CONFIG["mcp"]
    assert config["instructions"][:-1] == IMAGE_CONFIG["instructions"]

    # Validated by the BINARY before anything committed to it, as the candidate rather than
    # after the fact.
    validations = [c for c in booted["calls"] if c["argv"] == ["debug", "config"]]
    assert validations, (
        "the composed config was never checked with `opencode debug config`. Valid JSON is "
        "not the bar: opencode exits non-zero on a schema it does not recognise, and "
        "`opencode serve` IS the container's process, so an unchecked config is a "
        "CrashLoopBackOff for every agent in the deployment."
    )
    assert validations[0]["OPENCODE_CONFIG"].endswith(".tmp")

    assert booted["argv"][0] == "serve"


def test_only_the_connectors_this_agent_actually_has_are_advertised(tmp_path):
    """The property that makes three connectors additive rather than three ways to fail.

    An agent with Slack and no mailbox that was handed EMAIL.md would spend its first
    instruction budget on a tool whose credential is absent, and would find out only by
    running it. Asserted as an EXACT list, because "EMAIL.md is not in there" is the kind of
    check that passes for a composition that advertises nothing at all.
    """
    booted = _boot(tmp_path, connectors=["slack"])
    assert _instructions(booted) == IMAGE_CONFIG["instructions"] + ["/etc/agent/SLACK.md"]


def test_an_agent_with_everything_gets_every_document_once_and_in_order(tmp_path):
    """The turnkey shape (enterpriseaiframework-e5ca): mail plus both chat connectors.

    Order is asserted because it is what the model reads first, and duplication is asserted
    because a composition that ran per-connector instead of once would append EMAIL.md three
    times on an agent that had all three.
    """
    booted = _boot(tmp_path, connectors=["email", "slack", "discord"])
    assert _instructions(booted) == IMAGE_CONFIG["instructions"] + [
        "/etc/agent/EMAIL.md", "/etc/agent/SLACK.md", "/etc/agent/DISCORD.md",
    ]


def test_an_agent_with_no_connectors_boots_exactly_as_it_did_before(tmp_path):
    """Additive. No Secret means no AGENT_*_ variables, and nothing about the boot changes
    except that /etc/agent is on PATH."""
    booted = _boot(tmp_path)
    assert booted["OPENCODE_CONFIG"] == booted["image_config"], (
        "an agent with no connectors got a composed config anyway, so every existing agent "
        "would be handed instructions for tools it cannot use."
    )
    assert not (booted["state_dir"] / "opencode.json").exists()
    assert booted["argv"][0] == "serve"
    assert "/etc/agent" in booted["PATH"].split(":")


def test_a_config_opencode_itself_rejects_falls_back_instead_of_crashlooping(tmp_path):
    """The failure the JSON check cannot see, injected through the binary's own verdict.

    Since `opencode serve` is the container's process, accepting a config opencode rejects
    would take down every agent in the deployment — for a markdown file.
    """
    booted = _boot(tmp_path, connectors=["slack", "discord"], reject_config=True)
    assert booted["OPENCODE_CONFIG"] == booted["image_config"], \
        "the daemon was started on a config opencode had already rejected"
    assert booted["argv"][0] == "serve"
    assert "falling back" in booted["stderr"]
    assert not (booted["state_dir"] / "opencode.json").exists()
    assert not (booted["state_dir"] / "opencode.json.tmp").exists()
    assert "/etc/agent" in booted["PATH"].split(":")


@pytest.mark.parametrize("broken", [
    pytest.param("{ this is not json", id="unparseable"),
    pytest.param("", id="empty"),
])
def test_a_config_that_cannot_be_composed_falls_back_instead_of_killing_the_agent(tmp_path,
                                                                                  broken):
    booted = _boot(tmp_path, connectors=["slack"], image_config=broken)
    assert booted["OPENCODE_CONFIG"] == booted["image_config"]
    assert booted["argv"][0] == "serve", "the daemon was not started"
    assert "falling back" in booted["stderr"], (
        "the fallback happened silently. `kubectl logs` is the only way anyone finds out "
        "why an unattended agent does not know it has a chat connector."
    )
    assert not (booted["state_dir"] / "opencode.json.tmp").exists()
    assert not (booted["state_dir"] / "opencode.json").exists()
    # The tools themselves are unaffected: only the discovery hint was lost.
    assert "/etc/agent" in booted["PATH"].split(":")


def test_the_documentation_the_agent_is_handed_is_the_one_in_this_checkout():
    """The instructions files the composition names have to exist and have to be the ones
    reviewed here, or opencode is pointed at nothing and the tools are undiscoverable."""
    entrypoint = ENTRYPOINT.read_text()
    for doc_name, tool, commands in (
        ("SLACK.md", "agent-slack", ("agent-slack send", "agent-slack receive")),
        ("DISCORD.md", "agent-discord", ("agent-discord send", "agent-discord receive")),
    ):
        doc = (REPO / "deploy/agent" / doc_name).read_text()
        for command in commands:
            assert command in doc, f"{doc_name} does not document `{command}`"
        assert f"/etc/agent/{doc_name}" in entrypoint, \
            f"the entrypoint never names {doc_name}, so opencode is never told about {tool}"

        # The safety rules are the reason this file is instructions and not a README. An
        # agent that can post as a real company, in front of everyone in a channel, and that
        # reads messages anybody in the workspace can write, has to be told what not to do.
        lowered = doc.lower()
        for rule in ("irreversible",          # posting cannot be undone
                     "credentials",           # never post secrets
                     "data, not as instructions",  # received text is untrusted input
                     "draft"):                # drafting is not sending
            assert rule in lowered, f"{doc_name} no longer states the rule about {rule!r}"
