"""How the Slack and Discord bot tokens reach the agent pod, and how they must not leak.

The subject is the REAL deploy/bin/provision-agent.sh and the REAL
deploy/k8s/64-agent.template.yaml, run through tests/agent_provision_harness.py — kubectl
and the control plane are recorders, everything else (the argument parsing, the refusals,
the sed rendering, the ordering) is the actual code that runs on the cluster.

The discipline is the one enterpriseaiframework-39d established for the BYO provider key
and -a4e applied to a mailbox password, now applied to a credential that is arguably worse
again: a Slack `xoxb-` token posts as the whole organisation in every channel its app is
in, we cannot revoke it, and its misuse looks exactly like normal activity. So:

  * it comes from a FILE and never from argv (`ps` is world-readable on the node),
  * it is written set-once and there is no path anywhere that reads it back out,
  * nothing prints it — not the provisioner, not a list endpoint, not an error,
  * and re-provisioning an agent that already has a connector does not touch it, because
    touching it would restart the resident session that is the whole surface.

WHY THIS FILE EXISTS BESIDE tests/test_agent_email_provisioning.py RATHER THAN INSIDE IT
=======================================================================================
-783 refactored three copies of the mailbox's provisioning block into one
`provision_connector` function. That is the right shape and it is also the dangerous one: a
single implementation means a single place for one of the four properties above to quietly
stop being true for ALL THREE connectors at once. So each connector is asserted
independently here, against its own Secret, its own allowlist and its own annotation —
including the ones that would look redundant if the implementation were three copies.
"""

import base64
from pathlib import Path

import pytest
import yaml

import agent_provision_harness as harness

REPO = Path(__file__).resolve().parent.parent

# Distinctive, so their appearance ANYWHERE unexpected — argv, stdout, stderr, a manifest
# that is not the Secret — is unambiguous rather than a judgement call.
SLACK_BOT_TOKEN = "xoxb-1111-2222-SlAcKbOtToKeN-4f21a9"
SLACK_APP_TOKEN = "xapp-1-A012-3456-ApPlEvElToKeN-7c03e1"
DISCORD_BOT_TOKEN = "MTIzNDU2Nzg5.DiScOrDbOtToKeN.9b7f2c1e"

SLACK = f"""\
SLACK_BOT_TOKEN={SLACK_BOT_TOKEN}
SLACK_APP_TOKEN={SLACK_APP_TOKEN}
SLACK_HOME_CHANNEL=C0123ABCD
"""

DISCORD = f"""\
DISCORD_BOT_TOKEN={DISCORD_BOT_TOKEN}
DISCORD_HOME_CHANNEL=555000111222333444
"""

# label -> (flag, config text, the secret's required key, every token in that config)
CONNECTORS = {
    "slack": ("--slack-config-file", SLACK, "SLACK_BOT_TOKEN",
              (SLACK_BOT_TOKEN, SLACK_APP_TOKEN)),
    "discord": ("--discord-config-file", DISCORD, "DISCORD_BOT_TOKEN",
                (DISCORD_BOT_TOKEN,)),
}


def _config_file(tmp_path: Path, name: str, content: str) -> str:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / f"{name}.env"
    path.write_text(content)
    path.chmod(0o600)
    return str(path)


def _provision(tmp_path: Path, label: str, content: str | None = None, **kwargs):
    flag, default, _required, _tokens = CONNECTORS[label]
    text = default if content is None else content
    return harness.provision(tmp_path, "baron", "chatter",
                             flag, _config_file(tmp_path, label, text), **kwargs)


def _annotations(run) -> dict:
    return run.deployment()["spec"]["template"]["metadata"]["annotations"]


# ---------------------------------------------------------------- the credential lands

def test_the_slack_tokens_are_written_to_their_own_per_agent_secret(tmp_path):
    run = _provision(tmp_path, "slack")
    assert run.returncode == 0, run.output

    # Its OWN Secret, not the mailbox's and not the model credential's: the three have
    # different lifecycles (a Slack token is rotated in Slack's admin UI, a mail password in
    # M365's, a virtual key by minting at the gateway) and merging them would mean rotating
    # any one of them rewrites — and rolls the pod for — all three.
    assert run.secret_data("agent-baron-chatter-slack", "SLACK_BOT_TOKEN") == SLACK_BOT_TOKEN
    assert run.secret_data("agent-baron-chatter-slack", "SLACK_APP_TOKEN") == SLACK_APP_TOKEN
    assert run.secret_data("agent-baron-chatter-slack",
                           "SLACK_HOME_CHANNEL") == "C0123ABCD"
    assert run.secret_data("agent-baron-chatter-email", "SLACK_BOT_TOKEN") is None


def test_the_discord_token_is_written_to_its_own_per_agent_secret(tmp_path):
    run = _provision(tmp_path, "discord")
    assert run.returncode == 0, run.output
    assert run.secret_data("agent-baron-chatter-discord",
                           "DISCORD_BOT_TOKEN") == DISCORD_BOT_TOKEN
    assert run.secret_data("agent-baron-chatter-discord", "OPENAI_API_KEY") is None


@pytest.mark.parametrize("label", list(CONNECTORS))
def test_the_secret_is_created_with_flags_real_kubectl_actually_accepts(tmp_path, label):
    """`kubectl create secret` REFUSES --from-env-file alongside --from-file/--from-literal.

    Asserted per connector because the shared function could pass for one caller and not
    another if a future edit made the argv depend on the label. Enforced a second time
    inside the recorder itself, which is what makes this more than a grep.
    """
    run = _provision(tmp_path, label)
    assert run.returncode == 0, run.output
    creates = [line for line in run.calls.splitlines()
               if " create secret " in line and f"-{label}" in line]
    assert creates, f"the {label} Secret was never created"
    for line in creates:
        if "--from-env-file" in line:
            assert "--from-literal" not in line and "--from-file=" not in line, (
                f"kubectl will refuse this command on a real cluster: {line}"
            )


@pytest.mark.parametrize("label", list(CONNECTORS))
def test_no_bot_token_ever_reaches_argv_or_any_output(tmp_path, label):
    """`--from-env-file`, never `--from-literal`.

    argv is world-readable in `ps` for the life of the call, on a node that also runs other
    tenants' pods. A Slack bot token read out of `ps` posts as the customer's company.
    """
    _flag, _content, _required, tokens = CONNECTORS[label]
    run = _provision(tmp_path, label)
    assert run.returncode == 0, run.output

    for token in tokens:
        assert token not in run.calls, (
            "a bot token appeared in a command line. Every process on the node can read "
            "that out of `ps`:\n"
            + "\n".join(line for line in run.calls.splitlines() if token in line)
        )
        assert token not in run.output, (
            "the provisioner printed a bot token. This output goes to a terminal, a CI "
            "log, and whatever collects them."
        )
        # It is in the Secret, and the Secret is the only place it is.
        holders = [
            doc["metadata"]["name"] for doc in yaml.safe_load_all(run.applied)
            if doc and doc.get("kind") == "Secret"
            and token in {base64.b64decode(v).decode(errors="replace")
                          for v in (doc.get("data") or {}).values()}
        ]
        assert holders == [f"agent-baron-chatter-{label}"], holders


@pytest.mark.parametrize("label", list(CONNECTORS))
def test_nothing_ever_reads_a_bot_token_back(tmp_path, label):
    """Set-once. The only rotation is re-supplying the file.

    Asserted against the recorded argv rather than by reading the script, because the
    failure this guards is a future `kubectl get secret …-slack -o jsonpath={.data.
    SLACK_BOT_TOKEN}` added for a plausible reason — an idempotence check, a
    validation step — which would put the credential into a shell variable and from there
    into any error the script later printed.
    """
    _flag, _content, required, _tokens = CONNECTORS[label]
    sum_key = f"{label.upper()}_CONFIG_SUM"

    supplied = _provision(tmp_path / "a", label)
    rerun = harness.provision(tmp_path / "b", "baron", "chatter",
                              **{f"existing_{label}_sum": "deadbeefdeadbeef"})

    def reads(run):
        return [line for line in run.calls.splitlines()
                if " get secret " in line and f"-{label}" in line]

    for run in (supplied, rerun):
        for line in reads(run):
            assert required not in line, (
                f"the provisioner read a {label} credential back out of the cluster: {line}"
            )

    # The one thing it IS allowed to read back is the hash beside the credential, and only
    # on the path where it has to decide whether the connector already exists.
    assert any(sum_key in line for line in reads(rerun)), (
        f"the provisioner never checked for an existing {label} config, so a re-run cannot "
        "tell an agent that has one from an agent that does not."
    )


# ---------------------------------------------------------------- the pod is wired

def test_the_pod_takes_every_connector_as_optional_env(tmp_path):
    """`optional: true` on all three is what makes chat ADDITIVE.

    Every agent provisioned before -783 has no Slack or Discord Secret at all. Without
    `optional` each of them would wedge in ContainerCreating the moment this merged — on a
    surface whose whole premise is that nobody is watching it.
    """
    run = _provision(tmp_path, "slack")
    container = run.deployment()["spec"]["template"]["spec"]["containers"][0]

    sources = container["envFrom"]
    assert [s["secretRef"]["name"] for s in sources] == [
        "agent-baron-chatter-email",
        "agent-baron-chatter-slack",
        "agent-baron-chatter-discord",
    ]
    for source in sources:
        assert source["secretRef"]["optional"] is True, source

    # `env` must still win over `envFrom`, which is what stops a chat config file from
    # overriding the spendable model credential. Kubernetes guarantees the precedence; this
    # asserts we did not move it into envFrom and lose it. The opencode daemon's
    # OPENCODE_SERVER_PASSWORD is gone — `hermes gateway run` opens no inbound port.
    explicit = {e["name"] for e in container["env"]}
    assert "OPENAI_API_KEY" in explicit
    assert "OPENCODE_SERVER_PASSWORD" not in explicit, (
        "the opencode server password is retired; there is no inbound port to guard"
    )


@pytest.mark.parametrize("label", list(CONNECTORS))
def test_a_resupplied_bot_token_rolls_the_pod(tmp_path, label):
    """env from a Secret is injected at pod start and never updated afterwards.

    So a token change that did not restart the pod would leave the agent presenting the old
    one until something else happened to restart it — and a revoked Slack token fails as a
    silent `invalid_auth` on a surface whose whole premise is that nobody is watching.
    """
    _flag, content, _required, tokens = CONNECTORS[label]
    first = _provision(tmp_path / "a", label)
    rotated = content.replace(tokens[0], tokens[0][:10] + "-rotated")
    second = _provision(tmp_path / "b", label, rotated)

    assert _annotations(first)[f"checksum/{label}"] != _annotations(second)[f"checksum/{label}"]
    assert _annotations(second)[f"checksum/{label}"] != "none"


@pytest.mark.parametrize("label", list(CONNECTORS))
def test_reprovisioning_an_agent_with_a_connector_does_not_roll_it(tmp_path, label):
    """The property that makes re-running the provisioner safe on a live agent.

    Restarting an agent ends the resident session, which is the entire product, so a re-run
    that says nothing about chat must render the SAME annotation — even though the
    provisioner is forbidden to read the credential it would otherwise hash.
    """
    _flag, _content, required, _tokens = CONNECTORS[label]
    supplied = _provision(tmp_path / "a", label)
    written = supplied.secret_data(f"agent-baron-chatter-{label}",
                                   f"{label.upper()}_CONFIG_SUM")
    assert written and written != "none"
    assert _annotations(supplied)[f"checksum/{label}"] == written

    rerun = harness.provision(tmp_path / "b", "baron", "chatter",
                              **{f"existing_{label}_sum": written})
    assert rerun.returncode == 0, rerun.output
    assert _annotations(rerun)[f"checksum/{label}"] == written, (
        "re-provisioning changed a chat rollout annotation without being given a new "
        "credential. That restarts the agent and ends its session for no reason."
    )
    assert rerun.secret_data(f"agent-baron-chatter-{label}", required) is None
    assert "kept" in rerun.output


def test_an_agent_provisioned_without_chat_is_unchanged_and_says_so(tmp_path):
    run = harness.provision(tmp_path, "baron", "plain")
    assert run.returncode == 0, run.output
    assert _annotations(run)["checksum/slack"] == "none"
    assert _annotations(run)["checksum/discord"] == "none"
    assert run.secret_data("agent-baron-plain-slack", "SLACK_BOT_TOKEN") is None
    assert "no Slack workspace" in run.output and "no Discord guild" in run.output


def test_each_connector_is_independent_of_the_others(tmp_path):
    """Slack alone must not provision Discord, and must not roll its annotation either."""
    run = _provision(tmp_path, "slack")
    assert _annotations(run)["checksum/slack"] != "none"
    assert _annotations(run)["checksum/discord"] == "none"
    assert _annotations(run)["checksum/email"] == "none"
    # The pod still DECLARES the optional Discord source — that is what makes adding it
    # later one flag and one rollout rather than a template change — but no Discord Secret
    # was created, which is the thing that would have overwritten an existing one.
    created = [doc["metadata"]["name"] for doc in yaml.safe_load_all(run.applied)
               if doc and doc.get("kind") == "Secret"]
    assert "agent-baron-chatter-discord" not in created
    assert "agent-baron-chatter-email" not in created
    assert "agent-baron-chatter-slack" in created


def test_a_mailbox_and_both_chat_connectors_coexist_on_one_agent(tmp_path):
    """The turnkey shape (enterpriseaiframework-e5ca depends on it): all three at once."""
    mail = _config_file(tmp_path, "mailbox", """\
EMAIL_ADDRESS=ops@contoso.com
EMAIL_PASSWORD=mailbox-app-password
EMAIL_SMTP_HOST=smtp.office365.com
EMAIL_IMAP_HOST=outlook.office365.com
""")
    run = harness.provision(
        tmp_path, "baron", "chatter",
        "--email-config-file", mail,
        "--slack-config-file", _config_file(tmp_path, "slack", SLACK),
        "--discord-config-file", _config_file(tmp_path, "discord", DISCORD),
    )
    assert run.returncode == 0, run.output
    assert run.secret_data("agent-baron-chatter-email", "EMAIL_PASSWORD") == \
        "mailbox-app-password"
    assert run.secret_data("agent-baron-chatter-slack", "SLACK_BOT_TOKEN") == SLACK_BOT_TOKEN
    assert run.secret_data("agent-baron-chatter-discord",
                           "DISCORD_BOT_TOKEN") == DISCORD_BOT_TOKEN
    annotations = _annotations(run)
    # Three DIFFERENT checksums. One shared value would mean rotating any credential rolls
    # the pod for all of them and `kubectl describe` cannot say which one changed.
    assert len({annotations["checksum/email"], annotations["checksum/slack"],
                annotations["checksum/discord"]}) == 3


def test_the_template_has_no_unsubstituted_placeholders_left(tmp_path):
    """__SLACKSUM__ and __DISCORDSUM__ joined __EMAILSUM__; a placeholder the sed forgot
    renders as a literal `__SLACKSUM__` in a live manifest, which is a valid annotation
    value and therefore silent."""
    run = _provision(tmp_path, "slack")
    rendered = yaml.dump(run.deployment())
    assert "__" not in rendered, f"unsubstituted placeholder:\n{rendered}"


# ---------------------------------------------------------------- the refusals

@pytest.mark.parametrize("label", list(CONNECTORS))
@pytest.mark.parametrize("key", ["PATH", "LD_PRELOAD", "OPENAI_API_KEY", "AGENT_USER",
                                 "EMAIL_PASSWORD"])
def test_a_chat_config_cannot_smuggle_a_non_chat_variable_into_the_pod(tmp_path, label, key):
    """Every key in this file becomes an environment variable in the agent container.

    That container holds a spendable API key and runs unattended. `PATH=/tmp/evil` in a file
    called "Slack settings" is arbitrary code execution wearing a disguise, and the
    template's `env`-beats-`envFrom` precedence only defends the two names somebody happened
    to think of. EMAIL_PASSWORD is in the list on purpose: each connector's allowlist
    must be ITS OWN, or one shared function silently lets a Slack file write the mailbox.
    """
    _flag, content, _required, _tokens = CONNECTORS[label]
    run = _provision(tmp_path, label, content + f"{key}=whatever\n")

    assert run.returncode != 0, run.output
    assert key in run.output and "is not a" in run.output
    applied_secrets = [doc["metadata"]["name"] for doc in yaml.safe_load_all(run.applied)
                       if doc and doc.get("kind") == "Secret"]
    assert f"agent-baron-chatter-{label}" not in applied_secrets, \
        "the provisioner refused but had already applied the Secret"


def test_a_slack_config_with_only_a_bot_token_is_refused(tmp_path):
    """Socket Mode needs the app-level token. Without it the agent can post and can never
    hear an answer, which reads as "Slack is broken" long after the provisioning that caused
    it, with nothing connecting the two."""
    partial = f"SLACK_BOT_TOKEN={SLACK_BOT_TOKEN}\n"
    run = _provision(tmp_path, "slack", partial)
    assert run.returncode != 0, run.output
    assert "SLACK_APP_TOKEN" in run.output
    assert SLACK_BOT_TOKEN not in run.output


def test_a_slack_config_with_only_an_app_token_is_refused(tmp_path):
    run = _provision(tmp_path, "slack", f"SLACK_APP_TOKEN={SLACK_APP_TOKEN}\n")
    assert run.returncode != 0, run.output
    assert "SLACK_BOT_TOKEN" in run.output


def test_a_discord_config_with_no_token_is_refused(tmp_path):
    run = _provision(tmp_path, "discord", "DISCORD_HOME_CHANNEL=555\n")
    assert run.returncode != 0, run.output
    assert "DISCORD_BOT_TOKEN" in run.output


@pytest.mark.parametrize("label", list(CONNECTORS))
def test_a_file_that_is_not_key_equals_value_is_refused_without_echoing_it(tmp_path, label):
    """A refusal must not print the line it refused — the line may BE the token.

    The obvious implementation ("that line is malformed: <line>") is the one that leaks, and
    it leaks specifically in the case where somebody pasted a bare credential into the file
    by mistake.
    """
    _flag, content, _required, tokens = CONNECTORS[label]
    run = _provision(tmp_path, label, content + tokens[0] + "\n")
    assert run.returncode != 0, run.output
    assert "not KEY=value" in run.output
    assert tokens[0] not in run.output


@pytest.mark.parametrize("label", list(CONNECTORS))
def test_a_windows_line_ending_file_is_refused_with_the_actual_diagnosis(tmp_path, label):
    """kubectl stores values literally, so CRLF gives every setting a trailing \\r.

    The token becomes `xoxb-…\\r` (authentication failure) and presents as "Slack is broken"
    with nothing pointing at the file. An operator pasting a token out of a browser on
    Windows is the likely path in.
    """
    _flag, content, _required, tokens = CONNECTORS[label]
    run = _provision(tmp_path, label, content.replace("\n", "\r\n"))
    assert run.returncode != 0, run.output
    assert "CRLF" in run.output
    assert tokens[0] not in run.output
    assert f"agent-baron-chatter-{label}" not in [
        doc["metadata"]["name"] for doc in yaml.safe_load_all(run.applied)
        if doc and doc.get("kind") == "Secret"]


@pytest.mark.parametrize("label", list(CONNECTORS))
def test_an_unreadable_chat_config_file_is_refused(tmp_path, label):
    flag, _content, _required, _tokens = CONNECTORS[label]
    run = harness.provision(tmp_path, "baron", "chatter", flag, str(tmp_path / "nope.env"))
    assert run.returncode != 0
    assert "cannot read" in run.output


@pytest.mark.parametrize("label", list(CONNECTORS))
def test_comments_and_blank_lines_are_allowed_in_a_chat_config(tmp_path, label):
    """Because a real operator will annotate the file, and a refusal there would read as
    "the token is wrong" rather than "remove your comment"."""
    _flag, content, required, _tokens = CONNECTORS[label]
    run = _provision(tmp_path, label,
                     f"# the ops bot, token from the {label} admin console\n\n" + content)
    assert run.returncode == 0, run.output
    assert run.secret_data(f"agent-baron-chatter-{label}", required)


def test_chat_is_orthogonal_to_the_model_credential(tmp_path):
    """A BYO-model agent can have Slack, and neither decision constrains the other."""
    byo_key = tmp_path / "byo.key"
    byo_key.write_text("sk-users-own-provider-key\n")
    run = harness.provision(
        tmp_path, "baron", "chatter",
        "--byo-key-file", str(byo_key), "--byo-api-base", "https://api.example.com/v1",
        "--slack-config-file", _config_file(tmp_path, "slack", SLACK),
    )
    assert run.returncode == 0, run.output
    assert run.secret_data("agent-baron-chatter-byo", "OPENAI_API_KEY") == "sk-users-own-provider-key"
    assert run.secret_data("agent-baron-chatter-slack", "SLACK_BOT_TOKEN") == SLACK_BOT_TOKEN
    labels = run.deployment()["spec"]["template"]["metadata"]["labels"]
    assert labels["agent.enterprise-ai/model-source"] == "byo"
