"""The turnkey one-shot: one command, a resident agent, metered inference, company chat.

The subject is the REAL deploy/bin/hermes-up.sh, which invokes the REAL
deploy/bin/provision-agent.sh as a subprocess exactly as it does on a cluster. Only kubectl
and the control plane are recorders (tests/agent_provision_harness.py). Nothing about the
composition is modelled: the argument parsing, the defaults, the refusals, the manifests
that reach `kubectl apply`, and the in-pod checks are all the actual code that runs.

WHAT THIS FILE IS ACTUALLY FOR
==============================
`hermes-up.sh` implements almost nothing — residency, naming, the key mint and the
credential discipline are provision-agent.sh's, and they are tested in
test_agent_model_api_config.py and test_agent_chat_provisioning.py. Two things are new,
and they are the only things worth a test here:

  1. THE DEFAULTS. Integrated (gateway -> Forge, metered on the one bill) and Slack, chosen
     without the operator naming either. A turnkey command whose default is BYO, or whose
     default is no chat at all, is not the command that was asked for.

  2. THE REFUSAL TO PRINT READY OVER SOMETHING THAT WAS NOT OBSERVED. This is the half that
     can only be proven by breaking it, so most of this file injects a real failure — a
     Deployment that is not Available, a pod that is not Running, a chat tool whose tokens
     are missing, an opencode that was never told the tool exists, a gateway that answers
     000 — and asserts the word READY does not appear and the exit is non-zero. A validator
     nobody has watched refuse is a validator nobody has evidence works.

EXPECTED VALUES COME FROM THE OTHER SIDE OF THE SEAM, NOT FROM THE SCRIPT UNDER TEST.
The integrated gateway base is read out of provision-agent.sh (which defines it) rather
than copied from hermes-up.sh (which asserts it), and the alias grammar is Contract 1 of
docs/design/records/agents-surface.md. Otherwise a typo shared by the script and the test
would be green in both places.
"""

import re
from pathlib import Path

import pytest
import yaml

import agent_provision_harness as harness

REPO = Path(__file__).resolve().parent.parent
HERMES = REPO / "deploy/bin/hermes-up.sh"

# Distinctive, so their appearance ANYWHERE — argv, stdout, stderr, a manifest that is not
# the connector Secret — is unambiguous rather than a judgement call.
SLACK_BOT_TOKEN = "xoxb-9999-8888-HeRmEsBoTtOkEn-1a2b3c"
SLACK_APP_TOKEN = "xapp-1-A999-8888-HeRmEsApPtOkEn-4d5e6f"
DISCORD_BOT_TOKEN = "MTk4NzY1.HeRmEsDiScOrDtOkEn.7g8h9i"

SLACK_CONFIG = f"""\
AGENT_SLACK_BOT_TOKEN={SLACK_BOT_TOKEN}
AGENT_SLACK_APP_TOKEN={SLACK_APP_TOKEN}
AGENT_SLACK_DEFAULT_CHANNEL=C0123ABCD
"""

DISCORD_CONFIG = f"""\
AGENT_DISCORD_BOT_TOKEN={DISCORD_BOT_TOKEN}
AGENT_DISCORD_DEFAULT_CHANNEL=555000111222333444
"""

USER, NAME = "baron", "hermes"
OBJ = f"agent-{USER}-{NAME}"


def _gateway_base_from_the_provisioner() -> str:
    """The integrated base, read from the script that DEFINES it.

    provision-agent.sh sets GATEWAY_BASE and points OPENAI_API_BASE at it in integrated
    mode; hermes-up.sh only asserts that the pod really ended up there. Taking the expected
    value from the definition means a change to one side is a test failure rather than a
    matched pair of edits nobody notices.
    """
    text = (REPO / "deploy/bin/provision-agent.sh").read_text()
    match = re.search(r'^GATEWAY_BASE="([^"]+)"', text, re.M)
    assert match, "provision-agent.sh no longer defines GATEWAY_BASE"
    return match.group(1)


GATEWAY_BASE = _gateway_base_from_the_provisioner()
# Contract 1 of docs/design/records/agents-surface.md: `<username>::agents/<name>`.
ALIAS = f"{USER}::agents/{NAME}"


def _config_file(tmp_path: Path, name: str, content: str) -> str:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / f"{name}.env"
    path.write_text(content)
    path.chmod(0o600)
    return str(path)


def _ready(run) -> bool:
    """Did it print READY — the word on its own line, which is the claim being made.

    Deliberately not `"READY" in output`: every refusal this file provokes prints
    "NOT READY: ...", and a substring check would call all of them a success.
    """
    return any(line.strip() == "READY" for line in run.output.splitlines())


def _up(tmp_path: Path, *extra: str, chat: str | None = "slack",
        with_file: bool = True, **kwargs):
    args: list[str] = [USER, NAME]
    if chat is not None and chat != "slack":
        args += ["--chat", chat]
    if with_file:
        if chat == "discord":
            args += ["--discord-config-file",
                     _config_file(tmp_path, "discord", DISCORD_CONFIG)]
        else:
            args += ["--slack-config-file", _config_file(tmp_path, "slack", SLACK_CONFIG)]
    args += list(extra)
    return harness.hermes_up(tmp_path, *args, **kwargs)


def _deployment(run) -> dict:
    dep = run.deployment()
    assert dep is not None, "no Deployment was applied"
    return dep


def _container(dep: dict) -> dict:
    return dep["spec"]["template"]["spec"]["containers"][0]


def _env(dep: dict) -> dict:
    out = {}
    for entry in _container(dep)["env"]:
        out[entry["name"]] = entry.get("value") or entry.get("valueFrom")
    return out


# ---------------------------------------------------------------------------------------
# 1. The defaults: one command, and it is the integrated Forge path with Slack.
# ---------------------------------------------------------------------------------------

class TestTheOneCommand:
    def test_slack_is_the_default_when_chat_is_omitted(self, tmp_path):
        """`hermes-up.sh <user> <name> --slack-config-file F` — no --chat anywhere.

        The connector Secret that lands, and the in-pod tool that gets asked whether it is
        configured, must both be Slack's. Asserted from the applied manifests and from the
        recorded exec, not from the summary text, which is the thing most likely to be
        right while the behaviour is wrong.
        """
        run = _up(tmp_path)
        assert run.returncode == 0, run.output
        assert _ready(run), run.output

        # The Slack credential reached the cluster...
        assert run.secret_data(f"{OBJ}-slack", "AGENT_SLACK_BOT_TOKEN") == SLACK_BOT_TOKEN
        assert run.secret_data(f"{OBJ}-slack", "AGENT_SLACK_APP_TOKEN") == SLACK_APP_TOKEN
        # ...and Discord's did not. A default that provisions both is not a default.
        assert run.secret_data(f"{OBJ}-discord", "AGENT_DISCORD_BOT_TOKEN") is None

        # The in-pod validation asked SLACK's tool, and about SLACK's instructions doc.
        assert "agent-slack config" in run.calls
        assert "agent-discord config" not in run.calls
        assert "/etc/agent/SLACK.md" in run.calls
        assert "/etc/agent/DISCORD.md" not in run.calls

    def test_the_default_inference_path_is_integrated_forge_not_byo(self, tmp_path):
        """Contract 4's default, all the way through to the rendered pod.

        `integrated` means the agent's spend is a row on the one bill under
        `<user>::agents/<name>`. `byo` means it is not — legitimately, but only when
        declared. A turnkey command that silently produced the second would be finding 4's
        unmetered path with a friendly summary printed over it.
        """
        run = _up(tmp_path)
        assert run.returncode == 0, run.output

        dep = _deployment(run)
        labels = dep["spec"]["template"]["metadata"]["labels"]
        assert labels["agent.enterprise-ai/model-source"] == "integrated"

        env = _env(dep)
        assert env["OPENAI_API_BASE"] == GATEWAY_BASE
        assert env["OPENAI_BASE_URL"] == GATEWAY_BASE
        # The key comes from the minted-virtual-key Secret, not the BYO one.
        assert env["OPENAI_API_KEY"]["secretKeyRef"]["name"] == f"{OBJ}-key"

        # And the key really was minted through the control plane, under Contract 1's
        # alias — the provisioner's own assertion, exercised here because hermes-up.sh
        # composing it is exactly what must not get lost.
        assert f'"surface": "agents/{NAME}"' in (
            (run.stub_dir / "curl-bodies.txt").read_text()
        )
        assert f"{ALIAS} (minted)" in run.output

        # No BYO Secret was written, and the provisioner was told to remove any stale one.
        assert run.secret_data(f"{OBJ}-byo", "OPENAI_API_KEY") is None
        assert f"delete secret {OBJ}-byo" in run.calls

    def test_discord_is_wired_when_asked_for(self, tmp_path):
        run = _up(tmp_path, chat="discord")
        assert run.returncode == 0, run.output
        assert _ready(run), run.output

        assert run.secret_data(f"{OBJ}-discord",
                               "AGENT_DISCORD_BOT_TOKEN") == DISCORD_BOT_TOKEN
        assert run.secret_data(f"{OBJ}-slack", "AGENT_SLACK_BOT_TOKEN") is None
        assert "agent-discord config" in run.calls
        assert "/etc/agent/DISCORD.md" in run.calls
        assert "agent-slack config" not in run.calls

    def test_the_ready_summary_answers_the_five_questions_it_promises(self, tmp_path):
        """name, status, chat, the Forge path, and the console URL.

        The summary IS the deliverable — the command exists so an operator does not have to
        assemble this from four kubectl invocations — so its contents are asserted rather
        than left to whatever the last edit happened to leave behind.
        """
        run = _up(tmp_path, public_base_url="https://portal.example.ts.net:8443")
        assert run.returncode == 0, run.output
        summary = run.output.split("\nREADY\n", 1)[1]

        assert NAME in summary and OBJ in summary
        assert "Running" in summary
        assert "slack" in summary
        assert GATEWAY_BASE in summary and "Forge" in summary
        assert ALIAS in summary
        assert f"https://portal.example.ts.net:8443/agents/{NAME}/" in summary


# ---------------------------------------------------------------------------------------
# 2. It refuses to say READY over something it did not observe.
# ---------------------------------------------------------------------------------------

class TestItRefusesAFalseReady:
    """Every case here is a REAL injected failure at the place the real one happens.

    Each parametrisation changes exactly one seeded cluster answer from HEALTHY. What is
    asserted is the pair that matters: a non-zero exit AND the absence of the word READY.
    Either one alone is a bug — an exit code nobody reads on a screen that says READY is
    the failure this whole surface exists to prevent.
    """

    @pytest.mark.parametrize("cluster,expected", [
        # The rollout "succeeded" and the Deployment is still not Available. This is the
        # gap `kubectl rollout status` alone leaves, and the reason hermes-up.sh re-checks.
        ({"deployment-available": "False"}, "not Available"),
        ({"deployment-available": ""}, "not Available"),
        # The pod behind an Available Deployment can still be anything by the time anyone
        # looks; CrashLoopBackOff presents as a phase that is not Running.
        ({"pod-phase": "Pending"}, "not Running"),
        ({"pod-name": ""}, "no pod found"),
        # The tool is on PATH and cannot see its credential — the pod predates the Secret,
        # because envFrom is injected at pod start and never updated afterwards.
        ({"chat-config": '{"bot_token_set": false, "app_token_set": true}'},
         "bot_token_set"),
        # Slack posts with the bot token and RECEIVES over Socket Mode with the app token.
        # An agent that can talk and can never listen is not a wired agent.
        ({"chat-config": '{"bot_token_set": true, "app_token_set": false}'},
         "app_token_set"),
        # The tool is not on PATH at all: the entrypoint ConfigMap did not roll.
        ({"chat-config": "", "chat-config-rc": "3"}, "did not report a configuration"),
        # opencode was never told the tool exists. entrypoint.sh falls back to the image
        # config rather than CrashLooping every agent over a documentation file, so this
        # failure is SILENT on the cluster: the agent simply never reaches for chat.
        ({"doc-state": "DOC_MISSING"}, "never told about"),
        # The pod cannot reach the gateway at all — curl's connection failure.
        ({"gateway-out": f"000 {GATEWAY_BASE} glm-5.2@deepinfra"}, "'000'"),
        # The minted key is not live at the gateway.
        ({"gateway-out": f"401 {GATEWAY_BASE} glm-5.2@deepinfra"}, "'401'"),
        # The model name is not in the catalogue.
        ({"gateway-out": f"400 {GATEWAY_BASE} glm-5.2@deepinfra"}, "'400'"),
        # kubectl exec itself failed — no output at all to parse.
        ({"gateway-out": "", "gateway-rc": "1"}, "not 200"),
    ])
    def test_a_broken_step_is_a_non_zero_exit_and_no_ready(self, tmp_path, cluster,
                                                           expected):
        run = _up(tmp_path, cluster=cluster)
        assert run.returncode != 0, run.output
        assert not _ready(run), run.output
        assert "NOT READY" in run.output, run.output
        assert expected in run.output, run.output

    def test_a_200_from_somewhere_that_is_not_our_gateway_is_not_a_metered_agent(
            self, tmp_path):
        """The one failure a naive check would call a success.

        A BYO agent answers 200 here too — from the user's own provider, producing no
        gateway ledger row at all. `hermes-up.sh` reports agents as metered, so it has to
        assert WHERE the 200 came from and not merely that there was one.
        """
        run = _up(tmp_path, cluster={
            "gateway-out": "200 https://api.anthropic-example.invalid/v1 some-model",
        })
        assert run.returncode != 0, run.output
        assert not _ready(run), run.output
        assert "BYO" in run.output and "ledger" in run.output

    def test_every_in_pod_check_is_actually_performed(self, tmp_path):
        """The recorder answers `unrecognised in-pod script` with 127 on anything else.

        So a future edit that drops a check, or rewrites one into something that no longer
        probes what it claims to, fails here rather than passing on a stale seeded answer.
        """
        run = _up(tmp_path)
        assert run.returncode == 0, run.output
        execs = [line for line in run.calls.splitlines() if " exec " in line]
        assert len(execs) == 3, execs
        assert "unrecognised in-pod script" not in run.output


# ---------------------------------------------------------------------------------------
# 3. Credentials, and where they must never appear.
# ---------------------------------------------------------------------------------------

class TestCredentialHandling:
    @pytest.mark.parametrize("chat,tokens", [
        ("slack", (SLACK_BOT_TOKEN, SLACK_APP_TOKEN)),
        ("discord", (DISCORD_BOT_TOKEN,)),
    ])
    def test_the_chat_tokens_are_never_in_argv_and_never_printed(self, tmp_path, chat,
                                                                 tokens):
        """`ps` is world-readable on the node, and a bot token posts as the organisation.

        The same assertion tests/test_agent_chat_provisioning.py makes of the provisioner,
        repeated here because hermes-up.sh is now the front door: a wrapper that took the
        token as an argument and wrote a temp file would defeat the discipline underneath
        it while every test below it stayed green.
        """
        run = _up(tmp_path, chat=chat)
        assert run.returncode == 0, run.output
        for token in tokens:
            assert token not in run.calls, "a chat token reached argv"
            assert token not in run.output, "a chat token was printed"
        # What IS in argv is the path to the file, which is the whole point.
        assert f"--{chat}-config-file" not in run.calls  # the flag never reaches kubectl

    def test_hermes_never_handles_the_minted_key_itself(self, tmp_path):
        """The inference check runs INSIDE the pod, on the pod's own environment.

        So this script never reads the virtual key, never holds it, and never puts it in
        argv on the node: what `ps` shows on the host is the literal, unexpanded text
        `${OPENAI_API_KEY}`. (The provisioner does pass the key it just minted to
        `kubectl create secret --from-literal`; that is its documented, accepted cost and
        is not this script's to make worse.)
        """
        run = _up(tmp_path)
        assert run.returncode == 0, run.output
        execs = [line for line in run.calls.splitlines() if " exec " in line]
        gateway_check = "\n".join(
            run.calls.split("kubectl -n enterprise-ai exec")[-1].splitlines()
        )
        assert "${OPENAI_API_KEY}" in gateway_check, gateway_check
        for line in execs:
            assert harness.ISSUED_KEY not in line
        # And no `kubectl get secret ...OPENAI_API_KEY` beyond the provisioner's own
        # existing-key probe: hermes-up.sh adds no new path that reads a live key out.
        assert run.calls.count("jsonpath={.data.OPENAI_API_KEY}") == 1


# ---------------------------------------------------------------------------------------
# 4. Refusals that happen BEFORE anything is changed.
# ---------------------------------------------------------------------------------------

class TestPreflightRefusals:
    def _nothing_happened(self, run):
        """No manifest applied, no key minted. `kubectl apply` is never reached."""
        assert run.applied.strip() == "", run.applied
        bodies = run.stub_dir / "curl-bodies.txt"
        assert not bodies.exists() or "keys/issue" not in run.calls

    def test_no_chat_credential_and_none_supplied_is_refused_up_front(self, tmp_path):
        """A turnkey agent with no chat is not what this command promises.

        Validation would catch it afterwards too — but only after minting a key and
        rolling a pod, and "it failed, and it also changed things" is the worst shape a
        one-shot command can have.
        """
        run = _up(tmp_path, with_file=False)
        assert run.returncode != 0
        assert not _ready(run)
        assert "no slack credential" in run.output.lower()
        assert "--slack-config-file" in run.output
        assert "Nothing has been changed" in run.output
        self._nothing_happened(run)

    @pytest.mark.parametrize("env", [
        {"AGENT_BYO_API_BASE": "https://api.provider.invalid/v1"},
        {"AGENT_BYO_API_KEY": "sk-someone-elses-provider-key"},
    ])
    def test_a_byo_environment_is_refused_rather_than_silently_obeyed(self, tmp_path, env):
        """provision-agent.sh derives BYO from the ENVIRONMENT as well as from flags.

        So a stale `AGENT_BYO_API_BASE` in an operator's shell would produce an unmetered
        agent while this command's summary said "Forge, metered". That is precisely the
        silently-wrong outcome the summary is supposed to rule out, so it is a refusal
        with the alternative named.
        """
        run = _up(tmp_path, env=env)
        assert run.returncode != 0
        assert not _ready(run)
        assert "INTEGRATED path" in run.output
        assert "provision-agent.sh --byo-key-file" in run.output
        self._nothing_happened(run)

    @pytest.mark.parametrize("args,expected", [
        ([USER, NAME, "--chat", "discord", "--slack-config-file", "/dev/null"],
         "--slack-config-file given but --chat is discord"),
        ([USER, NAME, "--chat", "slack", "--discord-config-file", "/dev/null"],
         "--discord-config-file given but --chat is slack"),
        ([USER, NAME, "--chat", "mattermost"], "must be 'slack' or 'discord'"),
        ([USER, NAME, "--nonsense"], "unknown argument"),
    ])
    def test_contradictory_or_unknown_arguments_are_refused(self, tmp_path, args,
                                                            expected):
        run = harness.hermes_up(tmp_path, *args)
        assert run.returncode != 0
        assert expected in run.output, run.output
        self._nothing_happened(run)

    def test_a_refusal_from_the_composed_provisioner_is_not_swallowed(self, tmp_path):
        """Composition's own failure mode: a wrapper that keeps going after its subprocess
        refused, and then reports on an agent that was never built.

        provision-agent.sh owns several refusals hermes-up.sh must never have to know about
        — the name grammar, the RFC 1123 length cap, a control plane that issued no key.
        Here the agent name breaks Contract 1's slug rule, so the provisioner refuses; what
        is asserted is that the exit propagates, that its diagnosis survives, and above all
        that validation did not run and READY was not printed.
        """
        run = harness.hermes_up(
            tmp_path, USER, "Hermes_Prime",
            "--slack-config-file", _config_file(tmp_path, "slack", SLACK_CONFIG))
        assert run.returncode != 0
        assert not _ready(run)
        assert "refusing: agent name 'Hermes_Prime'" in run.output
        assert " exec " not in run.calls, "validated a pod the provisioner never built"

    def test_a_lone_discord_config_file_selects_discord_without_chat(self, tmp_path):
        """`--discord-config-file F` with no `--chat` is unambiguous, so it is obeyed.

        Slack being the default must not mean "provision a Slack agent that has no Slack"
        for the operator who named Discord the only way they were given.
        """
        run = harness.hermes_up(
            tmp_path, USER, NAME, "--discord-config-file",
            _config_file(tmp_path, "discord", DISCORD_CONFIG))
        assert run.returncode == 0, run.output
        assert _ready(run)
        assert "agent-discord config" in run.calls
        assert "agent-slack config" not in run.calls
        assert run.secret_data(f"{OBJ}-discord",
                               "AGENT_DISCORD_BOT_TOKEN") == DISCORD_BOT_TOKEN


# ---------------------------------------------------------------------------------------
# 5. Idempotent and non-disruptive — the property that makes re-running it safe.
# ---------------------------------------------------------------------------------------

class TestRerunning:
    def test_a_rerun_needs_no_credential_file_and_does_not_touch_the_credential(
            self, tmp_path):
        """Re-supplying the file is the only way to rotate; not supplying it must be safe.

        The connector's Secret is not re-applied and no key is minted, so the second run is
        a validation pass over a healthy agent — which is exactly what a turnkey command
        should be when it is run twice.
        """
        run = harness.hermes_up(
            tmp_path / "rerun", USER, NAME,
            existing_key=harness.ISSUED_KEY, existing_slack_sum="0123456789abcdef")
        assert run.returncode == 0, run.output
        assert _ready(run), run.output

        assert "keys/issue" not in run.calls, "a live agent's key was re-minted"
        assert "(kept; re-running does not rotate a live agent)" in run.output
        assert f"create secret generic {OBJ}-slack" not in run.calls
        assert "kept; re-supply --slack-config-file to rotate" in run.output

    def test_the_pod_is_not_rolled_by_a_rerun(self, tmp_path):
        """THE property, because restarting an agent ends the resident session.

        The pod template's annotations are what decide whether Kubernetes replaces the pod.
        Run 1 provisions with the credential file; run 2 provisions with nothing, against
        the state run 1 left. If every annotation renders identically, the second `kubectl
        apply` is a no-op and the resident agent keeps its session.
        """
        first = _up(tmp_path / "first")
        assert first.returncode == 0, first.output
        slack_sum = first.secret_data(f"{OBJ}-slack", "AGENT_SLACK_CONFIG_SUM")
        assert slack_sum, "the provisioner stored no slack sum to keep"

        second = harness.hermes_up(
            tmp_path / "second", USER, NAME,
            existing_key=harness.ISSUED_KEY, existing_slack_sum=slack_sum)
        assert second.returncode == 0, second.output

        a = _deployment(first)["spec"]["template"]["metadata"]["annotations"]
        b = _deployment(second)["spec"]["template"]["metadata"]["annotations"]
        assert a == b, f"a re-run would roll the pod:\n{yaml.dump(a)}\n{yaml.dump(b)}"
        assert a["checksum/slack"] == slack_sum
        assert a["checksum/discord"] == "none"


# ---------------------------------------------------------------------------------------
# 6. The file itself.
# ---------------------------------------------------------------------------------------

class TestTheScriptOnDisk:
    def test_it_is_executable_and_documented(self):
        assert HERMES.exists()
        assert HERMES.stat().st_mode & 0o111, "hermes-up.sh is not executable"
        header = HERMES.read_text()
        # The one-liner, both variants, in the file an operator opens first.
        assert "--slack-config-file" in header and "--chat discord" in header
        # And the live caveat: the deployed control plane predates the agents surface.
        assert "enterpriseaiframework-a39" in header

    def test_the_readme_shows_the_one_liner(self):
        readme = (REPO / "deploy/README.md").read_text()
        assert "hermes-up.sh" in readme
        assert "--chat discord" in readme

    def test_it_composes_the_provisioner_rather_than_reimplementing_it(self, tmp_path):
        """Reimplement nothing — asserted from what it DID, not from a grep of the source.

        The mint, the object naming, the manifest rendering and the set-once credential
        handling belong to provision-agent.sh, and there must be exactly one copy of each.
        A source-text check would be both weaker and wrong: this script legitimately names
        `deploy/k8s/63-agent-common.yaml` in a diagnostic, and a re-implementation could
        avoid every string on any list of forbidden ones.

        What actually distinguishes composing from reimplementing is that hermes-up.sh
        never WRITES to the cluster on its own account. provision-agent.sh's last act is
        the rollout wait; every call after that point is hermes-up.sh's, and every one of
        them must be a read (`get`) or an in-pod probe (`exec`).
        """
        assert "provision-agent.sh" in HERMES.read_text()

        run = _up(tmp_path)
        assert run.returncode == 0, run.output
        lines = run.calls.splitlines()
        cut = max(i for i, line in enumerate(lines) if "rollout status" in line)
        # One recorded invocation per line, EXCEPT that an argument may itself contain
        # newlines — the in-pod gateway probe is a multi-line shell script. Continuation
        # lines belong to the invocation above them.
        after = [line for line in lines[cut + 1:]
                 if line.startswith("kubectl ") or line.startswith("curl ")]
        assert after, "hermes-up.sh validated nothing after provisioning"
        for line in after:
            assert line.startswith("kubectl "), f"hermes-up.sh calls out itself: {line}"
            assert not any(f" {verb} " in line
                           for verb in ("apply", "create", "delete", "patch", "scale")), (
                f"hermes-up.sh writes to the cluster itself: {line}"
            )
            assert " get " in line or " exec " in line, line
