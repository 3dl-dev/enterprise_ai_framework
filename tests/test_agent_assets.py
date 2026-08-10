"""One list of agent-pod files, named in three places, checked for drift.

WHAT WENT WRONG, AND WHY A TEST RATHER THAN A COMMENT

The agent pod mounts ONE deployment-wide ConfigMap, `agent-entrypoint`, at `/etc/agent`:
the entrypoint plus every outside-world tool the entrypoint puts on PATH — `agent-email`,
`agent-slack`, `agent-discord`, the `agentws.py` module both chat tools import, and the
instruction files that tell opencode each tool exists.

Two things write that ConfigMap. `deploy/bin/provision-agent.sh` shipped all eight files.
`control-plane/app/agents.py`, when -627 made create self-serve, shipped `entrypoint.sh`
alone — and it writes with a server-side apply carrying `force=true`. So one user pressing
Create in the Agents tab did not merely get an agent without the chat tools: it REPLACED
the shared ConfigMap and removed `agent-slack`, `agent-email`, `agent-discord` and
`agentws.py` from every agent in the namespace, including ones an operator had provisioned
and wired days earlier. Their credentials stayed in their environments; the programs that
read them vanished. Found by running the connector endpoint against live k3s
(`tests-live/test_portal_connectors.py`): the credential arrived in the pod's environment
exactly as designed, and `/etc/agent/agent-slack config` exited 1 because the file was not
there.

`deploy/bin/deploy.sh` is the third place, because it builds the `agent-assets` ConfigMap
the control-plane pod reads these files FROM. A file missing there is one the control
plane cannot ship even when it means to.

Nothing about that failure is visible in a diff of any single file, which is why this is a
test and not a comment. All three lists are PARSED from their own source — a transcription
here would be a fourth copy with the same failure mode.
"""

import hashlib
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SHELL = REPO / "deploy" / "bin" / "provision-agent.sh"
DEPLOY = REPO / "deploy" / "bin" / "deploy.sh"
AGENTS_PY = REPO / "control-plane" / "app" / "agents.py"
AGENT_DIR = REPO / "deploy" / "agent"


def _shell_agent_files() -> tuple[str, ...]:
    """`AGENT_FILES=(...)` out of provision-agent.sh."""
    body = re.search(r"AGENT_FILES=\((.*?)\)", SHELL.read_text(), re.S)
    assert body, "provision-agent.sh no longer declares AGENT_FILES"
    return tuple(body.group(1).split())


def _python_agent_files() -> tuple[str, ...]:
    body = re.search(r"^AGENT_FILES = \((.*?)\)", AGENTS_PY.read_text(), re.S | re.M)
    assert body, "app/agents.py no longer declares AGENT_FILES"
    return tuple(re.findall(r'"([^"]+)"', body.group(1)))


def _deploy_agent_assets() -> tuple[str, ...]:
    """The `--from-file=` keys of the `agent-assets` ConfigMap in deploy.sh."""
    text = deploy_block()
    return tuple(re.findall(r"--from-file=([^=\s]+)=", text))


def deploy_block() -> str:
    text = DEPLOY.read_text()
    start = text.index("create configmap agent-assets")
    return text[start:text.index("kubectl apply -f -", start)]


def test_the_two_provisioning_paths_ship_the_same_files_in_the_same_order():
    """Same files, and the SAME ORDER, because the order decides the checksum.

    Both paths hash the concatenation of these files to produce `checksum/entrypoint` on
    the pod template. A different order is a different hash for identical bytes, which
    means provisioning an agent by either route would restart every agent created by the
    other — ending resident sessions, which is the one thing Contract 2 says a re-run must
    never do.
    """
    shell, python = _shell_agent_files(), _python_agent_files()
    assert python == shell, (
        "the control plane and provision-agent.sh disagree about what the agent pod "
        f"mounts.\n  provision-agent.sh: {shell}\n  app/agents.py:      {python}\n"
        "The ConfigMap is shared and written with force=true, so whichever list is "
        "shorter deletes the difference from every agent in the namespace."
    )


def test_the_control_plane_is_given_every_file_it_is_expected_to_ship():
    """deploy.sh's `agent-assets` must carry everything `app/agents.py` reads.

    The control-plane image is built from `control-plane/` alone; these files live under
    `deploy/`, so the pod only has what this ConfigMap gives it. A missing one is a 503 at
    create time — which is the good failure — but only because `asset()` refuses rather
    than substituting an embedded default.
    """
    assets = _deploy_agent_assets()
    for name in _python_agent_files():
        assert name in assets, (
            f"deploy.sh does not put {name} in the agent-assets ConfigMap, so the "
            "control-plane pod cannot ship it to an agent"
        )
    assert "64-agent.template.yaml" in assets, "the object template is not shipped"


def test_every_named_file_exists_in_the_repository():
    for name in _python_agent_files():
        assert (AGENT_DIR / name).is_file(), f"deploy/agent/{name} does not exist"


def test_the_shell_hashes_every_file_it_ships():
    """`CFGSUM` must be derived from the SAME array the ConfigMap is built from.

    It once hashed `entrypoint.sh` alone, which is a rollout trigger that quietly stops
    firing for every other file in the ConfigMap. The equality with the control plane's
    own checksum over the same eight files is asserted against a real rendered Deployment
    in `control-plane/tests/test_portal_connectors.py`, where the app is importable; this
    half only holds the shell to hashing what it ships.
    """
    text = SHELL.read_text()
    cfgsum = re.search(r"CFGSUM=\$\((.*?)\)\n", text, re.S)
    assert cfgsum, "provision-agent.sh no longer computes CFGSUM"
    assert 'for f in "${AGENT_FILES[@]}"' in cfgsum.group(1), (
        "CFGSUM is not derived from AGENT_FILES, so a file can be shipped without being "
        f"hashed: {cfgsum.group(1)!r}"
    )
    # And the value it would produce, from the repository, for the record.
    concatenated = b"".join((AGENT_DIR / name).read_bytes()
                            for name in _shell_agent_files())
    assert len(hashlib.sha256(concatenated).hexdigest()[:16]) == 16
