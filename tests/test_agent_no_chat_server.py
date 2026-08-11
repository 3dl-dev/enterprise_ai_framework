"""Baron's ruling on -a4e and -783, made mechanical, plus the agent's no-inbound-route shape.

These two invariants outlived the opencode connector tools (tests/test_agent_slack.py,
test_agent_discord.py, test_agent_email.py) that used to carry them. Those tools tested the
opencode shell CLIs that the Hermes retarget deleted — `hermes gateway run` reads its
native messaging connectors from the environment instead — so the CLI proofs are gone. But
two claims they asserted are properties of the DEPLOYMENT, not of any tool, and they are the
kind that erode by accretion, so they are kept here and checked against the manifests:

  1. There is no mail server and no chat server in any deploy manifest. The agent uses the
     tenant's EXISTING Slack workspace, Discord guild or IMAP+SMTP mailbox with the tenant's
     own credential. Somebody adding a "just for testing" Mattermost to a compose file is
     exactly how this decision quietly reverses.
  2. The agent pod publishes no inbound route. Under opencode the check was "the agent
     Service is ClusterIP with no NodePort"; the Hermes retarget removed the Service
     entirely (`hermes gateway run` opens no port and the console attaches over pods/exec),
     so the invariant is now the stronger "the template declares no Service and no container
     port at all". If either reappears, somebody has published a route to a pod that runs
     unattended next to a spendable model key.
"""

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
TEMPLATE = REPO / "deploy/k8s/64-agent.template.yaml"

# Deliberately the names of chat/mail SERVER products, not the words "slack"/"discord"/
# "email": the manifests legitimately name `agent-<user>-<name>-slack` Secrets and an
# `-email` connector, and a marker list that matched those would be a check nobody could
# keep green and everybody would delete.
CHAT_SERVER_MARKERS = (
    "mattermost", "rocketchat", "rocket.chat", "zulip", "synapse", "matrix-conduit",
    "ejabberd", "prosody", "openfire", "tinode", "revoltchat", "spacebarchat",
    "element-web", "jitsi-meet", "gitter", "zulipchat",
    # Mail servers — the -a4e ruling is the same shape: use the tenant's IMAP+SMTP, run none.
    "maddy", "stalwart", "postfix", "dovecot", "greenmail", "mailhog", "mailpit",
)


def _deploy_manifest_paths():
    for root in ("deploy", "bundle"):
        base = REPO / root
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if path.suffix in (".yaml", ".yml") and path.is_file():
                yield path


def test_no_chat_or_mail_server_component_exists_in_any_deploy_manifest():
    """The agent USES the tenant's Slack/Discord/mailbox; we run no chat or mail server.

    Checked against every manifest under deploy/ and bundle/ rather than remembered, because
    this is exactly the decision that erodes by accretion. Test fixtures do not count — they
    live in tests/, are started and torn down by pytest, and never appear in a manifest,
    which is the distinction this check enforces.
    """
    offenders = []
    for path in _deploy_manifest_paths():
        text = path.read_text(errors="replace").lower()
        for marker in CHAT_SERVER_MARKERS:
            if marker in text:
                offenders.append(f"{path.relative_to(REPO)}: {marker}")
    assert not offenders, (
        "a chat or mail server appeared in a deploy manifest:\n  " + "\n  ".join(offenders) +
        "\n\nThe ruling on enterpriseaiframework-783/-a4e is that the agent uses the "
        "tenant's EXISTING Slack workspace, Discord guild and IMAP+SMTP mailbox with the "
        "tenant's own credentials. We do not run chat or mail infrastructure. If a fixture "
        "is needed, it belongs in tests/."
    )


def test_the_agent_template_publishes_no_inbound_route():
    """Socket Mode / native connectors are why no inbound route is ever needed.

    `hermes gateway run` is outbound-only and the console attaches over the Kubernetes
    pods/exec subresource, so the agent needs — and gets — no Service and no container port.
    The opencode surface had a ClusterIP Service on 4096; the retarget removed it. A Service
    or a port reappearing here is somebody publishing a route to a pod that holds a spendable
    model key and runs with nobody watching it.
    """
    docs = [d for d in yaml.safe_load_all(TEMPLATE.read_text().replace("__", "x")) if d]

    services = [d for d in docs if d.get("kind") == "Service"]
    assert not services, (
        "the agent template declares a Service. `hermes gateway run` opens no inbound port "
        "and the console attaches over pods/exec; there is nothing to expose."
    )

    for deployment in (d for d in docs if d.get("kind") == "Deployment"):
        for container in deployment["spec"]["template"]["spec"]["containers"]:
            assert not container.get("ports"), (
                f"the agent container {container.get('name')!r} declares a port "
                f"{container.get('ports')!r}; the agent has no inbound route by design."
            )
