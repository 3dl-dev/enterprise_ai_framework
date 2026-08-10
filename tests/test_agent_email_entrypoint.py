"""The agent's boot has to hand opencode a mail tool, and must never trade the daemon for it.

Two claims, and the second is the one that keeps the surface alive:

  1. When the agent has a mailbox, `agent-email` is on PATH and opencode is told the tool
     exists — by COMPOSING the image's own opencode config with one extra `instructions`
     entry. The image cannot be edited (Contract 6 freezes deploy/workspace/, including
     the Dockerfile that bakes that config), so composition at boot is the only channel.
  2. When that composition fails for ANY reason, the agent still boots on the image's
     config. opencode 1.18.7 hard-errors and exits non-zero on a config it cannot parse,
     so a bad render would turn a missing documentation file into an agent that never
     starts — trading the entire resident surface for a markdown file.

The real deploy/agent/entrypoint.sh is executed. `opencode` is a recorder, because the
question is what the entrypoint hands it, and the pod's absolute paths are supplied
through the same environment variables the pod template sets.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
ENTRYPOINT = REPO / "deploy/agent/entrypoint.sh"

# A stand-in for the config baked into the workspace image. Shaped like the real one
# (deploy/workspace/opencode.json) in the parts the composition touches, so a change to
# the composition that lost the provider block is visible here.
IMAGE_CONFIG = {
    "$schema": "https://opencode.ai/config.json",
    "provider": {"enterprise-ai": {"npm": "@ai-sdk/openai-compatible",
                                   "options": {"baseURL": "{env:OPENAI_API_BASE}"}}},
    "model": "enterprise-ai/glm-5.2@deepinfra",
    "instructions": ["/etc/opencode/PLATFORM.md", "/etc/opencode/tenant/TENANT.md"],
    "mcp": {"echo": {"type": "remote", "url": "http://mcp-echo:8080/mcp", "enabled": True}},
}

# Records EVERY invocation (one JSON object per line), then exits instead of serving
# forever. Every call is recorded, not just the last, because the entrypoint now makes two
# — `debug config` to validate the composed file and `serve` to become the daemon — and
# the validation call is itself under test.
#
# $STUB_REJECT makes `debug config` exit non-zero, which is how the real binary reports a
# config it will not start on. That is the fault injection for the failure this whole
# fallback exists to survive.
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


def _boot(tmp_path: Path, *, image_config, mailbox: bool, reject_config=False) -> dict:
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
        "AGENT_NAME": "mailer",
        "OPENCODE_SERVER_PASSWORD": "not-a-real-password",
        "AGENT_WORKDIR": str(tmp_path / "work"),
        "XDG_DATA_HOME": str(tmp_path / "state"),
        "OPENCODE_CONFIG": str(config_path),
    }
    (tmp_path / "home").mkdir()
    if reject_config:
        env["STUB_REJECT"] = "1"
    if mailbox:
        # The one variable the entrypoint keys off: it arrives from the per-agent Secret
        # through the pod template's `envFrom`, so "has a mailbox" is decided by the
        # cluster and never by a flag in this file.
        env["AGENT_EMAIL_SMTP_HOST"] = "smtp.office365.com"

    proc = subprocess.run(["bash", str(ENTRYPOINT)], capture_output=True, text=True,
                          timeout=180, env=env, cwd=str(tmp_path))
    assert proc.returncode == 0, f"the entrypoint did not reach the daemon:\n{proc.stderr}"
    assert record.exists(), (
        f"the entrypoint never ran opencode:\n{proc.stdout}\n{proc.stderr}"
    )
    calls = [json.loads(line) for line in record.read_text().splitlines() if line.strip()]
    served = [c for c in calls if c["argv"][:1] == ["serve"]]
    assert served, f"the entrypoint never exec'd the daemon; calls were {calls}"
    return {**served[-1], "calls": calls,
            "stdout": proc.stdout, "stderr": proc.stderr,
            "state_dir": tmp_path / "state"}


def test_an_agent_with_a_mailbox_gets_the_tool_on_path_and_opencode_is_told_about_it(tmp_path):
    booted = _boot(tmp_path, image_config=IMAGE_CONFIG, mailbox=True)

    assert "/etc/agent" in booted["PATH"].split(":"), (
        "the mail tool's directory is not on PATH, so `agent-email` is 'command not "
        "found' to opencode's shell tool."
    )

    composed = Path(booted["OPENCODE_CONFIG"])
    assert composed != tmp_path / "image-opencode.json", \
        "opencode was pointed at the image config, so it was never told the tool exists"
    config = json.loads(composed.read_text())
    assert config["instructions"][-1] == "/etc/agent/EMAIL.md"

    # Everything else carried over from the image config, byte for byte in meaning. A
    # composition that rebuilt the file instead of extending it would silently fork the
    # provider block and the model catalogue the moment either changed in the image.
    assert config["provider"] == IMAGE_CONFIG["provider"]
    assert config["model"] == IMAGE_CONFIG["model"]
    assert config["mcp"] == IMAGE_CONFIG["mcp"]
    assert config["instructions"][:-1] == IMAGE_CONFIG["instructions"]

    # The composed file was validated by the BINARY before anything committed to it, and
    # validated as the candidate rather than after the fact.
    validations = [c for c in booted["calls"] if c["argv"] == ["debug", "config"]]
    assert validations, (
        "the composed config was never checked with `opencode debug config`. Valid JSON "
        "is not the bar: opencode exits non-zero on a schema it does not recognise, and "
        "`opencode serve` IS the container's process, so an unchecked config is a "
        "CrashLoopBackOff for every agent in the deployment."
    )
    assert validations[0]["OPENCODE_CONFIG"].endswith(".tmp"), (
        "the candidate was moved into place before it was validated, so a rejected config "
        "would already be the one on disk."
    )

    # And it is still the resident daemon that gets exec'd — the whole surface.
    assert booted["argv"][0] == "serve"
    assert "--hostname" in booted["argv"] and "0.0.0.0" in booted["argv"]


def test_a_config_opencode_itself_rejects_falls_back_instead_of_crashlooping(tmp_path):
    """The failure the JSON check cannot see, injected through the binary's own verdict.

    A composed file can be well-formed JSON and still be a config this opencode build
    refuses to start on. Since `opencode serve` is the container's process, accepting it
    would take down every agent in the deployment. The entrypoint must notice, discard the
    candidate, and start the daemon on the image's config.
    """
    booted = _boot(tmp_path, image_config=IMAGE_CONFIG, mailbox=True, reject_config=True)

    assert booted["OPENCODE_CONFIG"] == str(tmp_path / "image-opencode.json"), \
        "the daemon was started on a config opencode had already rejected"
    assert booted["argv"][0] == "serve"
    assert "falling back" in booted["stderr"]
    # Nothing rejected is left on the PVC to be picked up by a later boot.
    assert not (booted["state_dir"] / "opencode.json").exists()
    assert not (booted["state_dir"] / "opencode.json.tmp").exists()
    assert "/etc/agent" in booted["PATH"].split(":")


def test_the_documentation_the_agent_is_handed_is_the_one_in_this_checkout():
    """The instructions file the composition names has to exist and has to be the one
    reviewed here, or opencode is pointed at nothing and the tool is undiscoverable."""
    doc = (REPO / "deploy/agent/EMAIL.md").read_text()
    assert "agent-email send" in doc and "agent-email list" in doc
    # The safety rules are the reason this file is instructions and not a README. An agent
    # that can send mail from a real company's address must be told what not to do with it.
    for rule in ("irreversible", "credentials", "instructions"):
        assert rule in doc.lower(), f"the mail instructions no longer mention {rule}"
    assert "/etc/agent/EMAIL.md" in (REPO / "deploy/agent/entrypoint.sh").read_text()


def test_an_agent_without_a_mailbox_boots_exactly_as_it_did_before(tmp_path):
    """Additive. No mail Secret means no AGENT_EMAIL_*, and nothing about the boot changes
    except that /etc/agent is on PATH."""
    booted = _boot(tmp_path, image_config=IMAGE_CONFIG, mailbox=False)
    assert booted["OPENCODE_CONFIG"] == str(tmp_path / "image-opencode.json"), (
        "an agent with no mailbox got a composed config anyway, so every existing agent "
        "would be handed instructions for a tool it cannot use."
    )
    assert not (booted["state_dir"] / "opencode.json").exists()
    assert booted["argv"][0] == "serve"


@pytest.mark.parametrize("broken", [
    pytest.param("{ this is not json", id="unparseable"),
    pytest.param("", id="empty"),
])
def test_a_config_that_cannot_be_composed_falls_back_instead_of_killing_the_agent(tmp_path, broken):
    """The failure mode that would be catastrophic and silent.

    opencode exits non-zero on a config it cannot parse. If the composition wrote a broken
    file and pointed opencode at it, every agent in the deployment would CrashLoopBackOff
    on its next restart — for a documentation file. So a failed composition must leave the
    image's config in place, say so in the log, and carry on.
    """
    booted = _boot(tmp_path, image_config=broken, mailbox=True)

    assert booted["OPENCODE_CONFIG"] == str(tmp_path / "image-opencode.json")
    assert booted["argv"][0] == "serve", "the daemon was not started"
    assert "falling back" in booted["stderr"], (
        "the fallback happened silently. `kubectl logs` is the only way anyone finds out "
        "why an unattended agent does not know it has a mailbox."
    )
    # No half-written file left on the PVC to be picked up by a later boot.
    assert not (booted["state_dir"] / "opencode.json.tmp").exists()
    assert not (booted["state_dir"] / "opencode.json").exists()

    # The tool itself is unaffected: only the discovery hint was lost.
    assert "/etc/agent" in booted["PATH"].split(":")
