"""The resident agent can send and receive real email (enterpriseaiframework-a4e).

WHAT IS REAL HERE AND WHAT IS NOT
=================================
Real: the protocols, both directions, in both directions of the proof.

  * A throwaway GreenMail (Apache-2.0) container is stood up by this file, holds two real
    mailboxes, and speaks real SMTP and real IMAP on real sockets. This file owns it and
    tears it down.
  * The thing under test is `deploy/agent/agent-email` — the actual file the agent pod
    puts on PATH, run as a subprocess exactly the way opencode's shell tool would run it.
    Not imported, not monkeypatched, not a copy.
  * The SEND proof retrieves the message from the RECIPIENT'S mailbox with this test's own
    `imaplib` client. The expected values come out of that mailbox, never out of the code
    under test — if `agent-email send` printed `{"sent": true}` without opening a socket,
    the mailbox would be empty and this test would be red.
  * The READ proof injects the inbound message with this test's own `smtplib` client, i.e.
    a real SMTP transaction into the fixture. Nothing stubs a mailer, and nothing writes
    into a fake maildir.
  * TLS is proved with certificate VERIFICATION, against a CA this file generates: the
    fixture is given a server certificate signed by it, and the negative case — the same
    connection with that CA removed from the trust store — must be REFUSED. Otherwise
    "we use TLS" would be a claim about a code path nothing ever executed.

Not real, and named so it is not mistaken for real: **no live Microsoft 365 or Gmail
account is exercised**. Those need a tenant, a licence and an app password, which only a
human can supply; that is a prerequisite item, not something this suite can fake. What
this file proves is that the tool speaks the protocols those providers speak, in the
security modes they require (implicit TLS on 465/993, certificate verification on), which
is everything up to the credential itself.

ONE THING THIS FILE ASSERTS THAT IS NOT A TEST OF OUR CODE AT ALL
=================================================================
`test_no_mail_server_component_exists_in_any_deploy_manifest` is Baron's ruling made
mechanical: the agent USES an external provider and we do NOT run a mail server. That is
the kind of decision that erodes by accretion — somebody adds a "just for testing" MailHog
to a manifest and it is in production a month later — so it is checked rather than
remembered.
"""

import json
import os
import secrets
import shutil
import socket
import subprocess
import time
import uuid
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
CLI = REPO / "deploy/agent/agent-email"

# Apache-2.0, and a test fixture only — it is never deployed and never appears in a
# manifest. Pinned: a fixture that silently changes its protocol behaviour turns this
# suite into a flaky one, and "the mail server moved" is the last thing anyone would
# suspect.
GREENMAIL_IMAGE = "greenmail/standalone:2.1.9"

AGENT_LOGIN, AGENT_PASSWORD = "agent", "agentpw-" + secrets.token_hex(6)
AGENT_ADDRESS = "agent@agent.test"
PEER_LOGIN, PEER_PASSWORD = "peer", "peerpw-" + secrets.token_hex(6)
PEER_ADDRESS = "peer@peer.test"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run(args, env_extra, timeout=90) -> subprocess.CompletedProcess:
    """Run the real CLI as a subprocess, the way opencode's shell tool would.

    The environment is built from scratch rather than inherited-and-patched: the tool
    reads its whole configuration from AGENT_EMAIL_*, so a leaked variable from the
    developer's shell would be a test that passes for a reason the pod does not have.
    """
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "HOME": os.environ.get("HOME", "/tmp")}
    env.update(env_extra)
    return subprocess.run([str(CLI), *args], capture_output=True, text=True,
                          timeout=timeout, env=env)


class Mail:
    """The fixture's coordinates, plus the two clients the PROOFS use directly."""

    def __init__(self, container, workdir, ports):
        self.container = container
        self.workdir = workdir
        self.ports = ports
        self.ca_file = str(workdir / "ca.pem")

    # ----------------------------------------------------------- the tool's environment
    def env(self, *, tls: bool, login=AGENT_LOGIN, password=AGENT_PASSWORD,
            address=AGENT_ADDRESS, ca_file=True) -> dict:
        if tls:
            base = {
                "AGENT_EMAIL_SMTP_HOST": "localhost",
                "AGENT_EMAIL_SMTP_PORT": str(self.ports["smtps"]),
                "AGENT_EMAIL_SMTP_SECURITY": "ssl",
                "AGENT_EMAIL_IMAP_HOST": "localhost",
                "AGENT_EMAIL_IMAP_PORT": str(self.ports["imaps"]),
                "AGENT_EMAIL_IMAP_SECURITY": "ssl",
            }
            if ca_file:
                base["AGENT_EMAIL_CA_FILE"] = self.ca_file
        else:
            base = {
                "AGENT_EMAIL_SMTP_HOST": "127.0.0.1",
                "AGENT_EMAIL_SMTP_PORT": str(self.ports["smtp"]),
                "AGENT_EMAIL_SMTP_SECURITY": "none",
                "AGENT_EMAIL_IMAP_HOST": "127.0.0.1",
                "AGENT_EMAIL_IMAP_PORT": str(self.ports["imap"]),
                "AGENT_EMAIL_IMAP_SECURITY": "none",
            }
        base.update({
            "AGENT_EMAIL_ADDRESS": address,
            "AGENT_EMAIL_USERNAME": login,
            "AGENT_EMAIL_PASSWORD": password,
        })
        return base

    # -------------------------------------------- the test's OWN clients (the ground truth)
    def deliver(self, *, to: str, sender: str, subject: str, body: str) -> None:
        """Put a message in a mailbox over a REAL SMTP transaction from this test.

        This is the injection side of the READ proof. Deliberately smtplib and not a
        GreenMail management API: an API call would write straight into the server's
        store, and then "the agent read a message" would be proved against something no
        mail ever passed through.
        """
        import smtplib
        from email.message import EmailMessage

        message = EmailMessage()
        message["From"] = sender
        message["To"] = to
        message["Subject"] = subject
        message.set_content(body)
        with smtplib.SMTP("127.0.0.1", self.ports["smtp"], timeout=30) as conn:
            conn.send_message(message)

    def fetch_by_subject(self, *, login: str, password: str, subject: str, tries=30):
        """Retrieve a message from the fixture mailbox with this test's own IMAP client.

        This is the verification side of the SEND proof, and it is why the proof is a
        proof: every expected value asserted afterwards is read out of the mail server,
        not out of the process that claimed to have sent it.
        """
        import email
        import email.policy
        import imaplib

        for _ in range(tries):
            conn = imaplib.IMAP4("127.0.0.1", self.ports["imap"])
            try:
                conn.login(login, password)
                conn.select("INBOX", readonly=True)
                typ, data = conn.uid("search", None, "HEADER", "Subject", subject)
                assert typ == "OK", (typ, data)
                uids = data[0].split()
                if uids:
                    typ, fetched = conn.uid("fetch", uids[-1], "(RFC822)")
                    assert typ == "OK", (typ, fetched)
                    raw = next(p[1] for p in fetched if isinstance(p, tuple))
                    return email.message_from_bytes(raw, policy=email.policy.default)
            finally:
                try:
                    conn.logout()
                except Exception:
                    pass
            time.sleep(0.4)
        return None

    def server_log(self) -> str:
        return subprocess.run(["docker", "logs", self.container],
                              capture_output=True, text=True, timeout=60).stdout + \
               subprocess.run(["docker", "logs", self.container],
                              capture_output=True, text=True, timeout=60).stderr


@pytest.fixture(scope="session")
def mail(tmp_path_factory):
    """A throwaway real SMTP+IMAP server, owned and torn down by this file.

    NOT skipped when docker is missing — it is failed. `make test` already runs the whole
    bundle under docker compose, so an environment without docker cannot run this suite
    at all, and a skip here would report "email works" on a machine that never checked.
    """
    if shutil.which("docker") is None:
        pytest.fail(
            "docker is required: this suite proves SMTP and IMAP against a real mail "
            "server in a container it owns. There is no mocked-mailer fallback, because "
            "a mocked mailer proves nothing about a protocol."
        )

    workdir = tmp_path_factory.mktemp("greenmail")
    # The container runs as its own uid and has to write the converted keystore back here.
    os.chmod(workdir, 0o777)
    _make_certificates(workdir)
    _convert_keystore_to_jks(workdir)

    ports = {name: _free_port() for name in ("smtp", "imap", "smtps", "imaps")}
    container = f"agent-email-fixture-{uuid.uuid4().hex[:10]}"
    subprocess.run(["docker", "rm", "-f", container], capture_output=True, timeout=120)

    opts = " ".join([
        "-Dgreenmail.setup.test.all",
        # Without this GreenMail binds 127.0.0.1 INSIDE the container, so every published
        # port connects to nothing and the failure looks like the server never started.
        "-Dgreenmail.hostname=0.0.0.0",
        f"-Dgreenmail.users={AGENT_LOGIN}:{AGENT_PASSWORD}@agent.test,"
        f"{PEER_LOGIN}:{PEER_PASSWORD}@peer.test",
        "-Dgreenmail.tls.keystore.file=/ks/keystore.jks",
        "-Dgreenmail.tls.keystore.password=changeit",
        # Logs every protocol line the server saw. One test asserts on those lines: it is
        # the only evidence available from the SERVER's side that the bytes on the wire
        # were a real SMTP conversation carrying this exact message.
        "-Dgreenmail.verbose",
    ])
    proc = subprocess.run([
        "docker", "run", "-d", "--name", container,
        "-v", f"{workdir}:/ks:ro",
        "-p", f"127.0.0.1:{ports['smtp']}:3025",
        "-p", f"127.0.0.1:{ports['imap']}:3143",
        "-p", f"127.0.0.1:{ports['smtps']}:3465",
        "-p", f"127.0.0.1:{ports['imaps']}:3993",
        "-e", f"GREENMAIL_OPTS={opts}",
        GREENMAIL_IMAGE,
    ], capture_output=True, text=True, timeout=600)
    assert proc.returncode == 0, f"could not start the mail fixture: {proc.stderr}"

    fixture = Mail(container, workdir, ports)
    try:
        _wait_until_serving(fixture)
        yield fixture
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True, timeout=180)


def _make_certificates(workdir: Path) -> None:
    """A CA and a server certificate with real SANs, so verification can actually pass.

    GreenMail's own bundled certificate has no subjectAltName at all, and Python has
    ignored CN since 3.7 — so with the stock fixture certificate the ONLY way to connect
    would be to disable hostname checking, and a test that disables verification cannot
    prove verification works. Generating a CA here is what makes both the positive and
    the negative case meaningful.
    """
    def openssl(*args, **kw):
        proc = subprocess.run(["openssl", *args], capture_output=True, text=True,
                              timeout=120, cwd=str(workdir), **kw)
        assert proc.returncode == 0, f"openssl {args[0]} failed: {proc.stderr}"

    openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "ca.key",
            "-out", "ca.pem", "-days", "3650", "-subj", "/CN=agent-email-test-ca")
    openssl("req", "-newkey", "rsa:2048", "-nodes", "-keyout", "server.key",
            "-out", "server.csr", "-subj", "/CN=localhost")
    (workdir / "san.cnf").write_text("subjectAltName=DNS:localhost,IP:127.0.0.1\n")
    openssl("x509", "-req", "-in", "server.csr", "-CA", "ca.pem", "-CAkey", "ca.key",
            "-CAcreateserial", "-out", "server.pem", "-days", "3650",
            "-extfile", "san.cnf")
    openssl("pkcs12", "-export", "-inkey", "server.key", "-in", "server.pem",
            "-certfile", "ca.pem", "-name", "greenmail", "-out", "keystore.p12",
            "-passout", "pass:changeit")
    os.chmod(workdir / "keystore.p12", 0o644)


def _convert_keystore_to_jks(workdir: Path) -> None:
    """GreenMail's SSL factory loads a JKS and rejects a PKCS#12, so convert with its own
    JDK's keytool — no JDK is needed on the host to run this suite."""
    # The chmod runs INSIDE the container: keytool writes the file as the image's own uid,
    # which the host user does not own and therefore cannot chmod. It has to be readable
    # because the server container mounts the same directory read-only and runs as that
    # same uid — but a 0600 file written by a one-shot container is a permission error at
    # server start, reported as "cannot load keystore" with no hint of why.
    proc = subprocess.run([
        "docker", "run", "--rm", "-v", f"{workdir}:/ks", "--entrypoint", "sh",
        GREENMAIL_IMAGE, "-c",
        "keytool -importkeystore -noprompt "
        "-srckeystore /ks/keystore.p12 -srcstoretype PKCS12 -srcstorepass changeit "
        "-destkeystore /ks/keystore.jks -deststoretype JKS "
        "-deststorepass changeit -destkeypass changeit && chmod 0644 /ks/keystore.jks",
    ], capture_output=True, text=True, timeout=300)
    assert (workdir / "keystore.jks").exists(), \
        f"keystore conversion failed: {proc.stdout}{proc.stderr}"


def _wait_until_serving(fixture: Mail, timeout=180) -> None:
    """All four services, including a real TLS handshake against the generated CA.

    The TLS legs are waited on separately because GreenMail starts them AFTER the
    plaintext ones (the keystore has to load first) — polling only SMTP and IMAP would
    hand the first TLS test a port that is listening but not yet serving, which is a
    flake, and a flake in a fixture is the worst kind to diagnose.
    """
    import imaplib
    import smtplib
    import ssl

    deadline = time.time() + timeout
    last = None
    context = ssl.create_default_context(cafile=fixture.ca_file)
    while time.time() < deadline:
        try:
            with smtplib.SMTP("127.0.0.1", fixture.ports["smtp"], timeout=10) as conn:
                conn.ehlo()
            plain_imap = imaplib.IMAP4("127.0.0.1", fixture.ports["imap"], timeout=10)
            plain_imap.login(AGENT_LOGIN, AGENT_PASSWORD)
            plain_imap.logout()
            for tls_port in (fixture.ports["smtps"], fixture.ports["imaps"]):
                with socket.create_connection(("localhost", tls_port), 10) as raw:
                    with context.wrap_socket(raw, server_hostname="localhost"):
                        pass
            return
        except Exception as exc:
            last = exc
        time.sleep(1)
    raise AssertionError(
        f"mail fixture never came up: {last}\n"
        + subprocess.run(["docker", "logs", fixture.container],
                         capture_output=True, text=True, timeout=60).stderr[-3000:]
    )


# ===========================================================================
# THE PROOF: real SMTP out, real IMAP in.
# ===========================================================================

def test_the_agent_sends_a_message_and_it_arrives_in_the_recipients_real_mailbox(mail):
    """SEND. Asserted from the RECIPIENT'S mailbox, not from what the tool printed."""
    subject = f"agent-send-{uuid.uuid4().hex[:12]}"
    body = f"Filed the report. Reference {uuid.uuid4().hex}."

    sent = _run(["send", "--to", PEER_ADDRESS, "--subject", subject, "--body", body],
                mail.env(tls=False))
    assert sent.returncode == 0, sent.stderr
    reported = json.loads(sent.stdout)
    assert reported["sent"] is True

    delivered = mail.fetch_by_subject(login=PEER_LOGIN, password=PEER_PASSWORD,
                                      subject=subject)
    assert delivered is not None, (
        f"the tool reported success but no message with subject {subject!r} is in "
        f"{PEER_ADDRESS}'s mailbox. Either nothing was transmitted, or it was "
        "transmitted somewhere else."
    )
    # Every value below comes out of the mail server.
    assert str(delivered["Subject"]) == subject
    assert str(delivered["From"]) == AGENT_ADDRESS
    assert str(delivered["To"]) == PEER_ADDRESS
    assert body in delivered.get_body(preferencelist=("plain",)).get_content()
    # The handle the tool handed back has to identify the message that actually arrived,
    # or "did my mail get there" is unanswerable for the agent that sent it.
    assert str(delivered["Message-ID"]) == reported["message_id"]


def test_the_send_was_a_real_authenticated_smtp_conversation_on_the_wire(mail):
    """The server's own view of it.

    The test above proves a message ARRIVED. This one proves HOW: GreenMail's verbose log
    records each protocol line it received, so the SMTP verbs and the authentication of
    this exact conversation are readable from the server side. Without this, a tool that
    somehow deposited the message by another route would pass the test above.
    """
    subject = f"agent-wire-{uuid.uuid4().hex[:12]}"
    sent = _run(["send", "--to", PEER_ADDRESS, "--subject", subject, "--body", "on the wire"],
                mail.env(tls=False))
    assert sent.returncode == 0, sent.stderr
    assert mail.fetch_by_subject(login=PEER_LOGIN, password=PEER_PASSWORD,
                                 subject=subject) is not None

    log = mail.server_log()
    assert "C: AUTH PLAIN" in log, (
        "the mail host never saw an AUTH command. Submission to a real provider is "
        "always authenticated; a tool that skips AUTH works against a permissive fixture "
        "and fails against M365 and Gmail."
    )
    # The envelope, as the server logged the bytes it received. Verbs are lower-case
    # because that is literally what Python's smtplib puts on the wire (`mail FROM:`),
    # and matching what was actually sent is the point of reading the server's log at all.
    assert f"C: mail FROM:<{AGENT_ADDRESS}>" in log
    assert f"C: rcpt TO:<{PEER_ADDRESS}>" in log
    assert "C: data" in log, "no DATA command — nothing was transmitted"
    assert f"C: Subject: {subject}" in log, \
        "the message never traversed the SMTP DATA command"


def test_the_agent_reads_inbound_mail_delivered_over_real_smtp(mail):
    """READ. The inbound message is injected by this test's own real SMTP transaction."""
    subject = f"inbound-{uuid.uuid4().hex[:12]}"
    body = f"The Q3 report is due Friday. Token {uuid.uuid4().hex}."
    mail.deliver(to=AGENT_ADDRESS, sender="boss@corp.test", subject=subject, body=body)

    listed = None
    for _ in range(30):
        out = _run(["list", "--limit", "50"], mail.env(tls=False))
        assert out.returncode == 0, out.stderr
        found = [m for m in json.loads(out.stdout) if m["subject"] == subject]
        if found:
            listed = found[0]
            break
        time.sleep(0.4)
    assert listed is not None, f"the agent's mailbox never showed {subject!r}"
    assert listed["from"] == "boss@corp.test"
    assert listed["to"] == AGENT_ADDRESS
    assert listed["seen"] is False, "listing mail must not mark it read"

    read = _run(["read", "--uid", listed["uid"]], mail.env(tls=False))
    assert read.returncode == 0, read.stderr
    message = json.loads(read.stdout)
    assert message["subject"] == subject
    assert body in message["body"], "the body the agent read is not the body that was sent"
    assert message["seen"] is False, (
        "reading without --mark-seen changed the \\Seen flag. An agent answering a "
        "question about the inbox must not silently mark a person's unread mail as read."
    )


def test_unseen_filter_and_mark_seen_are_the_two_states_they_claim_to_be(mail):
    subject = f"unseen-{uuid.uuid4().hex[:12]}"
    mail.deliver(to=AGENT_ADDRESS, sender="boss@corp.test", subject=subject,
                 body="please read me")

    uid = None
    for _ in range(30):
        out = _run(["list", "--unseen", "--limit", "50"], mail.env(tls=False))
        assert out.returncode == 0, out.stderr
        found = [m for m in json.loads(out.stdout) if m["subject"] == subject]
        if found:
            uid = found[0]["uid"]
            break
        time.sleep(0.4)
    assert uid is not None, "a freshly delivered message did not show up as unseen"

    marked = _run(["read", "--uid", uid, "--mark-seen"], mail.env(tls=False))
    assert marked.returncode == 0, marked.stderr

    out = _run(["list", "--unseen", "--limit", "50"], mail.env(tls=False))
    still_unseen = [m for m in json.loads(out.stdout) if m["subject"] == subject]
    assert not still_unseen, "--mark-seen did not mark the message read on the server"


# ===========================================================================
# TLS — the mode Microsoft 365 and Gmail actually require.
# ===========================================================================

def test_send_and_read_over_implicit_tls_with_a_verified_certificate(mail):
    """465/993 with certificate verification on: the shape a real provider needs.

    The plaintext tests above prove the protocol logic. This proves the transport the
    tool will actually use in production, including that the certificate chain is checked
    — which is only meaningful alongside the refusal test below.
    """
    subject = f"tls-{uuid.uuid4().hex[:12]}"
    body = f"encrypted in transit {uuid.uuid4().hex}"

    sent = _run(["send", "--to", AGENT_ADDRESS, "--subject", subject, "--body", body],
                mail.env(tls=True))
    assert sent.returncode == 0, sent.stderr

    got = None
    for _ in range(30):
        out = _run(["list", "--limit", "50"], mail.env(tls=True))
        assert out.returncode == 0, out.stderr
        found = [m for m in json.loads(out.stdout) if m["subject"] == subject]
        if found:
            got = found[0]
            break
        time.sleep(0.4)
    assert got is not None, "the message sent over SMTPS never appeared over IMAPS"

    read = _run(["read", "--uid", got["uid"]], mail.env(tls=True))
    assert read.returncode == 0, read.stderr
    assert body in json.loads(read.stdout)["body"]


def test_a_certificate_that_does_not_verify_is_refused_on_both_legs(mail):
    """The negative that makes the positive worth something.

    Same server, same ports, same everything — except the CA is not trusted. Both legs
    must refuse. An unattended agent holding a mailbox credential is exactly the caller
    who would never notice an interception, because there is no human watching the
    session, so there is deliberately no switch that turns this off.
    """
    out = _run(["check"], mail.env(tls=True, ca_file=False))
    assert out.returncode == 0, out.stderr  # `check` reports, it does not raise
    report = json.loads(out.stdout)
    assert report["ok"] is False
    for leg in ("smtp", "imap"):
        assert report[leg]["ok"] is False, f"{leg} accepted an unverifiable certificate"
        assert "CERTIFICATE_VERIFY_FAILED" in report[leg]["error"], report[leg]


# ===========================================================================
# The credential must not leak, and a missing one must not crash.
# ===========================================================================

def test_the_password_never_appears_in_output_even_when_the_login_is_rejected(mail):
    """The failure path is where credentials leak, because that is where servers echo.

    imaplib raises with the server's own rejection line, and some hosts include the login
    they were handed. So the wrong-password case is asserted, not the happy path.
    """
    password = "wrong-" + secrets.token_hex(8)
    env = mail.env(tls=False, password=password)

    for args in (["check"], ["list"], ["send", "--to", PEER_ADDRESS,
                                       "--subject", "x", "--body", "y"]):
        out = _run(args, env)
        combined = out.stdout + out.stderr
        assert password not in combined, (
            f"`agent-email {' '.join(args)}` printed the mailbox password. This output "
            "lands in an agent transcript that a person may later read and that may be "
            "shipped to a log collector."
        )
        assert "Traceback" not in combined, (
            f"`agent-email {' '.join(args)}` raised instead of reporting. A traceback "
            "puts the connection object, and on some paths the credential, into the "
            "transcript."
        )

    assert json.loads(_run(["check"], env).stdout)["ok"] is False


def test_config_reports_the_mailbox_without_ever_printing_the_password(mail):
    out = _run(["config"], mail.env(tls=False))
    assert out.returncode == 0, out.stderr
    config = json.loads(out.stdout)
    assert config["address"] == AGENT_ADDRESS
    assert config["password_set"] is True
    assert AGENT_PASSWORD not in out.stdout
    assert "password" not in json.dumps(config).replace("password_set", "")


def test_an_agent_with_no_mailbox_says_so_instead_of_crashing():
    """An agent provisioned without --email-config-file has no AGENT_EMAIL_* at all.

    The pod template mounts the mail Secret `optional: true` precisely so that agent still
    starts, so the tool has to answer for that state in a way a model can act on rather
    than dying with a KeyError.
    """
    config = json.loads(_run(["config"], {}).stdout)
    assert config["password_set"] is False
    assert config["smtp"]["host"] is None

    out = _run(["send", "--to", "a@b.test", "--subject", "s", "--body", "b"], {})
    assert out.returncode == 1
    assert "Traceback" not in out.stdout + out.stderr
    error = json.loads(out.stderr)["error"]
    assert "AGENT_EMAIL_ADDRESS" in error and "provision-agent.sh" in error, error


# ===========================================================================
# Baron's ruling, made mechanical.
# ===========================================================================

MAIL_SERVER_MARKERS = (
    "maddy", "stalwart", "postfix", "exim", "dovecot", "mailu", "mailcow",
    "mailhog", "greenmail", "james", "mailpit", "opensmtpd",
)


def _deploy_manifest_paths():
    for root in ("deploy", "bundle"):
        for path in sorted((REPO / root).rglob("*")):
            if path.suffix in (".yaml", ".yml") and path.is_file():
                yield path


def test_no_mail_server_component_exists_in_any_deploy_manifest():
    """Baron's ruling on -a4e: the agent USES an external provider; we run no mail server.

    Checked against every manifest under deploy/ and bundle/ rather than remembered,
    because this is exactly the decision that erodes by accretion: somebody adds a
    "temporary" MailHog to a compose file and it is in production a month later. The
    fixture in THIS file is a test fixture — it lives in tests/, is started by pytest and
    torn down by pytest, and never appears in a manifest, which is the distinction the
    check enforces.
    """
    offenders = []
    for path in _deploy_manifest_paths():
        text = path.read_text(errors="replace").lower()
        for marker in MAIL_SERVER_MARKERS:
            if marker in text:
                offenders.append(f"{path.relative_to(REPO)}: {marker}")
    assert not offenders, (
        "a mail server appeared in a deploy manifest:\n  " + "\n  ".join(offenders) +
        "\n\nThe ruling on enterpriseaiframework-a4e is that the agent uses an EXTERNAL "
        "mail provider (M365 / Gmail / any IMAP+SMTP host) with the tenant's own mailbox. "
        "We do not run mail infrastructure. If a fixture is needed, it belongs in tests/."
    )


def test_the_agent_can_reach_an_external_mail_provider_but_still_not_the_cluster():
    """Email needs egress, and the existing agent policy already grants exactly the right
    kind: the whole internet MINUS every private range.

    Asserted rather than assumed, because this feature depends on it: if a later change
    narrowed agent egress to named ports, mail would stop working, and the symptom would
    be an unattended agent silently failing to send. The same rule is what keeps that
    egress from reaching the API server, another agent, or a workspace.
    """
    policy = next(
        doc for doc in yaml.safe_load_all(
            (REPO / "deploy/k8s/63-agent-common.yaml").read_text())
        if doc and doc.get("kind") == "NetworkPolicy"
    )
    external = [
        rule for rule in policy["spec"]["egress"]
        if any("ipBlock" in to and to["ipBlock"]["cidr"] == "0.0.0.0/0"
               for to in rule.get("to", []))
    ]
    assert external, "agents have no route to an external mail provider at all"
    assert len(external) == 1, "more than one internet egress rule; which one governs mail?"

    rule = external[0]
    assert "ports" not in rule, (
        "the internet egress rule now restricts ports. SMTP submission (587/465) and "
        "IMAP (993) must be reachable or an agent's mailbox silently stops working."
    )
    excluded = set(rule["to"][0]["ipBlock"]["except"])
    for private in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16"):
        assert private in excluded, (
            f"{private} is no longer excluded from agent egress — mail reachability must "
            "not have been bought by opening a route to the cluster."
        )
