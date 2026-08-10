"""How the mailbox credential reaches the agent pod, and how it must not leak on the way.

The subject is the REAL deploy/bin/provision-agent.sh and the REAL
deploy/k8s/64-agent.template.yaml, run through tests/agent_provision_harness.py — kubectl
and the control plane are recorders, everything else (the argument parsing, the refusals,
the sed rendering, the ordering) is the actual code that runs on the cluster.

The discipline being asserted is the one enterpriseaiframework-39d established for the BYO
provider key, applied to a credential that is arguably worse to lose: a mailbox app
password sends mail AS a real person at a real company, we cannot revoke it, and unlike an
API key its misuse looks exactly like normal activity. So:

  * it comes from a FILE and never from argv (`ps` is world-readable on the node),
  * it is written set-once and there is no path anywhere that reads it back out,
  * nothing prints it — not the provisioner, not a list endpoint, not an error,
  * and re-provisioning an agent that already has a mailbox does not touch it, because
    touching it would restart the resident session that is the whole surface.

The last one has a subtlety worth stating: the pod's rollout annotation MUST change when
the credential changes (env from a Secret is injected at pod start and never updated, so a
re-supplied password that did not roll the pod would leave the agent authenticating with
the old one forever, silently). But the provisioner may not read the credential back to
hash it. That is why the hash lives in the Secret beside it — and why
`test_reprovisioning_an_agent_with_a_mailbox_does_not_roll_it` is the test that would
catch its absence.
"""

import base64
import subprocess
from pathlib import Path

import pytest
import yaml

import agent_provision_harness as harness

REPO = Path(__file__).resolve().parent.parent
TEMPLATE = REPO / "deploy/k8s/64-agent.template.yaml"

# A plausible Microsoft 365 mailbox. The password is distinctive so its appearance
# ANYWHERE unexpected — argv, stdout, stderr, a manifest that is not the Secret — is
# unambiguous rather than a judgement call.
MAILBOX_PASSWORD = "mAiLbOx-App-Password-8f31c7d0"
MAILBOX = f"""\
AGENT_EMAIL_ADDRESS=ops-agent@contoso.com
AGENT_EMAIL_USERNAME=ops-agent@contoso.com
AGENT_EMAIL_PASSWORD={MAILBOX_PASSWORD}
AGENT_EMAIL_SMTP_HOST=smtp.office365.com
AGENT_EMAIL_SMTP_PORT=587
AGENT_EMAIL_SMTP_SECURITY=starttls
AGENT_EMAIL_IMAP_HOST=outlook.office365.com
AGENT_EMAIL_IMAP_PORT=993
AGENT_EMAIL_IMAP_SECURITY=ssl
"""


def _config_file(tmp_path: Path, content: str = MAILBOX) -> str:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "mailbox.env"
    path.write_text(content)
    path.chmod(0o600)
    return str(path)


def _configmap(run, name: str) -> dict | None:
    for doc in yaml.safe_load_all(run.applied):
        if doc and doc.get("kind") == "ConfigMap" and doc["metadata"]["name"] == name:
            return doc
    return None


def _annotations(run) -> dict:
    return run.deployment()["spec"]["template"]["metadata"]["annotations"]


# ---------------------------------------------------------------- the credential lands

def test_the_mailbox_is_written_to_its_own_per_agent_secret(tmp_path):
    run = harness.provision(tmp_path, "baron", "mailer",
                            "--email-config-file", _config_file(tmp_path))
    assert run.returncode == 0, run.output

    # Its OWN Secret, not the one holding the model credential: a mailbox and an API key
    # have different lifecycles (one is rotated by re-supplying a file, the other by
    # minting at the gateway) and merging them would mean rotating either one rewrites
    # both.
    assert run.secret_data("agent-baron-mailer-email", "AGENT_EMAIL_PASSWORD") == MAILBOX_PASSWORD
    assert run.secret_data("agent-baron-mailer-email", "AGENT_EMAIL_ADDRESS") == "ops-agent@contoso.com"
    assert run.secret_data("agent-baron-mailer-email", "AGENT_EMAIL_SMTP_HOST") == "smtp.office365.com"
    assert run.secret_data("agent-baron-mailer-email", "AGENT_EMAIL_IMAP_HOST") == "outlook.office365.com"
    # The model credential is untouched by any of this.
    assert run.secret_data("agent-baron-mailer-email", "OPENAI_API_KEY") is None


def test_the_secret_is_created_with_flags_real_kubectl_actually_accepts(tmp_path):
    """`kubectl create secret` REFUSES --from-env-file alongside --from-file/--from-literal.

    This is here because the first version of the provisioner did exactly that. The
    harness's recorder accepted it, every test in this file went green, and the command
    would have died on the first real cluster with "from-env-file cannot be combined with
    from-file or from-literal" — a mock being more permissive than the tool it stands in
    for, which is the failure mode that deletes a test rather than simplifying it.
    Verified against real kubectl; asserted here on the argv the provisioner actually
    used, and enforced a second time inside the recorder itself.
    """
    run = harness.provision(tmp_path, "baron", "mailer",
                            "--email-config-file", _config_file(tmp_path))
    assert run.returncode == 0, run.output

    creates = [line for line in run.calls.splitlines()
               if " create secret " in line and "-email" in line]
    assert creates, "the mail Secret was never created"
    for line in creates:
        if "--from-env-file" in line:
            assert "--from-literal" not in line and "--from-file=" not in line, (
                f"kubectl will refuse this command on a real cluster: {line}"
            )


def test_the_mailbox_password_never_reaches_argv_or_any_output(tmp_path):
    """`--from-env-file`, never `--from-literal`.

    argv is world-readable in `ps` for the life of the call, on a node that also runs
    other tenants' pods. deploy/bin/provision-workspace.sh accepts that cost for a virtual
    key we minted and can revoke in one call; it is not acceptable for a credential on the
    customer's own mail tenancy that we cannot revoke at all.
    """
    run = harness.provision(tmp_path, "baron", "mailer",
                            "--email-config-file", _config_file(tmp_path))
    assert run.returncode == 0, run.output

    assert MAILBOX_PASSWORD not in run.calls, (
        "the mailbox password appeared in a command line. Every process on the node can "
        "read that out of `ps`:\n"
        + "\n".join(line for line in run.calls.splitlines() if MAILBOX_PASSWORD in line)
    )
    assert MAILBOX_PASSWORD not in run.output, (
        "the provisioner printed the mailbox password. This output goes to a terminal, a "
        "CI log, and whatever collects them."
    )
    # It is in the Secret, and the Secret is the only place it is.
    applied_secrets = [
        doc for doc in yaml.safe_load_all(run.applied)
        if doc and doc.get("kind") == "Secret"
        and MAILBOX_PASSWORD in {base64.b64decode(v).decode(errors="replace")
                                 for v in (doc.get("data") or {}).values()}
    ]
    assert [s["metadata"]["name"] for s in applied_secrets] == ["agent-baron-mailer-email"]


def test_nothing_ever_reads_the_mailbox_password_back(tmp_path):
    """Set-once, like the BYO key. The only rotation is re-supplying the file.

    Asserted against the recorded argv rather than by reading the script, because the
    failure this guards is a future `kubectl get secret …-email -o jsonpath={.data.
    AGENT_EMAIL_PASSWORD}` added for a plausible reason — an idempotence check, a
    validation step — which would put the credential into a shell variable and from there
    into any error the script later printed.
    """
    def reads_of_the_mail_secret(run):
        return [line for line in run.calls.splitlines()
                if " get secret " in line and "-email" in line]

    supplied = harness.provision(tmp_path / "a", "baron", "mailer",
                                 "--email-config-file", _config_file(tmp_path / "a"))
    rerun = harness.provision(tmp_path / "b", "baron", "mailer",
                              existing_email_sum="deadbeefdeadbeef")

    for run in (supplied, rerun):
        for line in reads_of_the_mail_secret(run):
            assert "AGENT_EMAIL_PASSWORD" not in line, (
                f"the provisioner read the mailbox password back out of the cluster: {line}"
            )

    # The one thing it IS allowed to read back is the hash beside the credential, and only
    # on the path where it has to decide whether a mailbox already exists.
    assert any("AGENT_EMAIL_CONFIG_SUM" in line for line in reads_of_the_mail_secret(rerun)), (
        "the provisioner never checked for an existing mailbox, so a re-run cannot tell "
        "an agent that has one from an agent that does not."
    )


# ---------------------------------------------------------------- the pod is wired

def test_the_pod_takes_the_mailbox_as_optional_env_so_agents_without_one_still_start(tmp_path):
    run = harness.provision(tmp_path, "baron", "mailer",
                            "--email-config-file", _config_file(tmp_path))
    container = run.deployment()["spec"]["template"]["spec"]["containers"][0]

    sources = container["envFrom"]
    # The mailbox, plus the chat connectors that joined it in enterpriseaiframework-783.
    # Exact and ordered, not a subset: an envFrom source nobody meant to add is a Secret
    # whose every key becomes an environment variable in this container.
    assert [s["secretRef"]["name"] for s in sources] == [
        "agent-baron-mailer-email",
        "agent-baron-mailer-slack",
        "agent-baron-mailer-discord",
    ]
    for source in sources:
        assert source["secretRef"]["optional"] is True, (
            f"{source['secretRef']['name']} is mounted as required. Every agent "
            "provisioned before that feature existed would wedge in ContainerCreating the "
            "moment it merged."
        )

    # `env` must still win over `envFrom`, which is what stops a mail config file from
    # overriding the spendable model credential or the daemon's own password. Kubernetes
    # guarantees the precedence; this asserts we did not move them into envFrom and lose
    # it.
    explicit = {e["name"] for e in container["env"]}
    assert {"OPENAI_API_KEY", "OPENCODE_SERVER_PASSWORD"} <= explicit


def test_the_mail_tool_is_delivered_to_the_pod_and_is_executable(tmp_path):
    """The CLI travels in the entrypoint ConfigMap, mounted 0755.

    The image is the workspace image byte-for-byte (Contract 6), so the tool cannot be
    baked in; it arrives the same way the entrypoint does. 0644 would leave it present, on
    PATH, and "permission denied" — the most confusing possible way for a tool to be
    missing.
    """
    run = harness.provision(tmp_path, "baron", "mailer",
                            "--email-config-file", _config_file(tmp_path))

    configmap = _configmap(run, "agent-entrypoint")
    assert configmap is not None, "the entrypoint ConfigMap was never applied"
    assert set(configmap["data"]) == {
        "entrypoint.sh",
        "agent-email", "EMAIL.md",
        # enterpriseaiframework-783 added the chat connectors to the same ConfigMap, for
        # the reason the provisioner records: they roll together with the entrypoint that
        # puts them on PATH.
        "agent-slack", "SLACK.md",
        "agent-discord", "DISCORD.md",
        "agentws.py",
    }

    shipped = base64.b64decode(configmap["data"]["agent-email"]).decode()
    assert shipped == (REPO / "deploy/agent/agent-email").read_text(), \
        "the ConfigMap does not carry the agent-email in this checkout"

    volume = next(v for v in run.deployment()["spec"]["template"]["spec"]["volumes"]
                  if v["name"] == "entrypoint")
    assert volume["configMap"]["defaultMode"] == 0o755, (
        "the entrypoint volume is not executable, so `agent-email` on PATH is a "
        "permission error rather than a tool."
    )

    entrypoint = base64.b64decode(configmap["data"]["entrypoint.sh"]).decode()
    assert "/etc/agent" in entrypoint and "PATH=" in entrypoint, \
        "the entrypoint does not put the mail tool on PATH"


def test_editing_the_mail_tool_rolls_the_agents(tmp_path):
    """The rollout annotation covers all three files in the ConfigMap, not just one.

    Expected value derived here from the source files directly, so a provisioner that went
    back to hashing entrypoint.sh alone — leaving a changed agent-email sitting in a
    ConfigMap that no pod ever re-read — fails this rather than passing quietly.
    """
    run = harness.provision(tmp_path, "baron", "mailer",
                            "--email-config-file", _config_file(tmp_path))
    expected = subprocess.run(
        "cat deploy/agent/entrypoint.sh deploy/agent/agent-email deploy/agent/EMAIL.md "
        "deploy/agent/agent-slack deploy/agent/SLACK.md deploy/agent/agent-discord "
        "deploy/agent/DISCORD.md deploy/agent/agentws.py "
        "| sha256sum | cut -c1-16",
        shell=True, capture_output=True, text=True, cwd=str(REPO), timeout=60,
    ).stdout.strip()
    assert _annotations(run)["checksum/entrypoint"] == expected


def test_a_resupplied_mailbox_rolls_the_pod(tmp_path):
    """env from a Secret is injected at pod start and never updated afterwards.

    So a password change that did not restart the pod would leave the agent presenting the
    old one until something else happened to restart it — on a surface whose whole premise
    is that nobody is watching.
    """
    first = harness.provision(tmp_path / "a", "baron", "mailer",
                              "--email-config-file", _config_file(tmp_path / "a"))
    rotated = MAILBOX.replace(MAILBOX_PASSWORD, "a-different-app-password")
    second = harness.provision(tmp_path / "b", "baron", "mailer",
                               "--email-config-file", _config_file(tmp_path / "b", rotated))

    assert _annotations(first)["checksum/email"] != _annotations(second)["checksum/email"]
    assert _annotations(second)["checksum/email"] != "none"


def test_reprovisioning_an_agent_with_a_mailbox_does_not_roll_it(tmp_path):
    """The property that makes re-running the provisioner safe on a live agent.

    Restarting an agent ends the resident session, which is the entire product, so a
    re-run that says nothing about email must render the SAME annotation — even though the
    provisioner is forbidden to read the credential it would otherwise hash.
    """
    supplied = harness.provision(tmp_path / "a", "baron", "mailer",
                                 "--email-config-file", _config_file(tmp_path / "a"))
    sum_written = supplied.secret_data("agent-baron-mailer-email", "AGENT_EMAIL_CONFIG_SUM")
    assert sum_written and sum_written != "none"
    assert _annotations(supplied)["checksum/email"] == sum_written

    rerun = harness.provision(tmp_path / "b", "baron", "mailer",
                              existing_email_sum=sum_written)
    assert rerun.returncode == 0, rerun.output
    assert _annotations(rerun)["checksum/email"] == sum_written, (
        "re-provisioning changed the mail rollout annotation without being given a new "
        "credential. That restarts the agent and ends its session for no reason."
    )
    # And it did not rewrite the Secret, so it cannot have clobbered the credential with
    # anything — including an empty one.
    assert rerun.secret_data("agent-baron-mailer-email", "AGENT_EMAIL_PASSWORD") is None
    assert "kept" in rerun.output


def test_an_agent_provisioned_without_a_mailbox_is_unchanged_and_says_so(tmp_path):
    run = harness.provision(tmp_path, "baron", "plain")
    assert run.returncode == 0, run.output
    assert _annotations(run)["checksum/email"] == "none"
    assert run.secret_data("agent-baron-plain-email", "AGENT_EMAIL_PASSWORD") is None
    assert "no mailbox" in run.output
    # The pod still declares the optional source, so adding a mailbox later is one
    # `--email-config-file` and one rollout, not a template change.
    container = run.deployment()["spec"]["template"]["spec"]["containers"][0]
    assert container["envFrom"][0]["secretRef"]["optional"] is True


# ---------------------------------------------------------------- the refusals

@pytest.mark.parametrize("key", ["PATH", "LD_PRELOAD", "OPENAI_API_KEY", "AGENT_USER"])
def test_a_mail_config_cannot_smuggle_a_non_mail_variable_into_the_pod(tmp_path, key):
    """Every key in this file becomes an environment variable in the agent container.

    That container holds a spendable API key and runs unattended. `PATH=/tmp/evil` in a
    file called "mail settings" is arbitrary code execution wearing a disguise, and the
    template's `env`-beats-`envFrom` precedence only defends the two names somebody
    happened to think of.
    """
    path = _config_file(tmp_path, MAILBOX + f"{key}=whatever\n")
    run = harness.provision(tmp_path, "baron", "mailer", "--email-config-file", path)

    assert run.returncode != 0, run.output
    assert key in run.output and "not a mail setting" in run.output
    assert "agent-baron-mailer-email" not in run.applied, \
        "the provisioner refused but had already applied the Secret"


@pytest.mark.parametrize("missing", [
    "AGENT_EMAIL_IMAP_HOST", "AGENT_EMAIL_SMTP_HOST",
    "AGENT_EMAIL_PASSWORD", "AGENT_EMAIL_ADDRESS",
])
def test_half_a_mailbox_is_refused_rather_than_provisioned(tmp_path, missing):
    """Send-only or read-only is not a configuration this surface offers.

    An agent that can send and cannot read reads as "email is broken" to whoever finds it,
    long after the provisioning that caused it and with nothing connecting the two.
    """
    partial = "".join(l + "\n" for l in MAILBOX.splitlines()
                      if not l.startswith(missing + "="))
    run = harness.provision(tmp_path, "baron", "mailer",
                            "--email-config-file", _config_file(tmp_path, partial))
    assert run.returncode != 0, run.output
    assert missing in run.output


def test_a_file_that_is_not_key_equals_value_is_refused_without_echoing_it(tmp_path):
    """A refusal must not print the line it refused — the line may BE the password.

    The obvious implementation of this check ("that line is malformed: <line>") is the one
    that leaks, and it leaks specifically in the case where somebody pasted a bare
    credential into the file by mistake.
    """
    run = harness.provision(tmp_path, "baron", "mailer", "--email-config-file",
                            _config_file(tmp_path, MAILBOX + MAILBOX_PASSWORD + "\n"))
    assert run.returncode != 0, run.output
    assert "not KEY=value" in run.output
    assert MAILBOX_PASSWORD not in run.output


def test_a_windows_line_ending_file_is_refused_with_the_actual_diagnosis(tmp_path):
    """kubectl stores values literally, so CRLF gives every setting a trailing \\r.

    The host becomes `outlook.office365.com\\r` (DNS failure) and the password becomes
    `secret\\r` (authentication failure), and both present as "email is broken" with
    nothing pointing at the file that caused it. An operator pasting an app password out
    of a Windows admin console is the likely path in.
    """
    run = harness.provision(tmp_path, "baron", "mailer", "--email-config-file",
                            _config_file(tmp_path, MAILBOX.replace("\n", "\r\n")))
    assert run.returncode != 0, run.output
    assert "CRLF" in run.output
    assert MAILBOX_PASSWORD not in run.output
    assert "agent-baron-mailer-email" not in run.applied


def test_an_unreadable_mail_config_file_is_refused(tmp_path):
    run = harness.provision(tmp_path, "baron", "mailer",
                            "--email-config-file", str(tmp_path / "nope.env"))
    assert run.returncode != 0
    assert "cannot read" in run.output


def test_comments_and_blank_lines_are_allowed_in_a_mail_config(tmp_path):
    """Because a real operator will annotate the file, and a refusal there would read as
    "the credential is wrong" rather than "remove your comment"."""
    annotated = "# the ops mailbox, app password from the M365 admin centre\n\n" + MAILBOX
    run = harness.provision(tmp_path, "baron", "mailer",
                            "--email-config-file", _config_file(tmp_path, annotated))
    assert run.returncode == 0, run.output
    assert run.secret_data("agent-baron-mailer-email", "AGENT_EMAIL_PASSWORD") == MAILBOX_PASSWORD


def test_the_mailbox_is_orthogonal_to_the_model_credential(tmp_path):
    """A BYO-model agent can have a mailbox, and neither decision constrains the other."""
    byo_key = tmp_path / "byo.key"
    byo_key.write_text("sk-users-own-provider-key\n")
    run = harness.provision(
        tmp_path, "baron", "mailer",
        "--byo-key-file", str(byo_key), "--byo-api-base", "https://api.example.com/v1",
        "--email-config-file", _config_file(tmp_path),
    )
    assert run.returncode == 0, run.output
    assert run.secret_data("agent-baron-mailer-byo", "OPENAI_API_KEY") == "sk-users-own-provider-key"
    assert run.secret_data("agent-baron-mailer-email", "AGENT_EMAIL_PASSWORD") == MAILBOX_PASSWORD
    labels = run.deployment()["spec"]["template"]["metadata"]["labels"]
    assert labels["agent.enterprise-ai/model-source"] == "byo"


def test_the_template_has_no_unsubstituted_placeholders_left(tmp_path):
    """__EMAILSUM__ was added to the template; a placeholder the sed forgot renders as a
    literal `__EMAILSUM__` in a live manifest, which is a valid annotation value and
    therefore silent."""
    run = harness.provision(tmp_path, "baron", "mailer",
                            "--email-config-file", _config_file(tmp_path))
    rendered = yaml.dump(run.deployment())
    assert "__" not in rendered, f"unsubstituted placeholder in the rendered Deployment:\n{rendered}"
