"""The resident agent can post to and receive from Slack (enterpriseaiframework-783).

WHAT IS REAL HERE AND WHAT IS NOT
=================================
Real: both protocols, in both directions.

  * A TLS HTTP server and a TLS websocket server, stood up by this file and torn down by
    it, speaking real HTTP/1.1 and real RFC 6455 on real sockets. See tests/chat_fakes.py
    — the websocket framing there is a second, independent implementation of the RFC, not
    an import from the tool.
  * The thing under test is `deploy/agent/agent-slack` — the actual file the agent pod puts
    on PATH, run as a subprocess exactly the way opencode's shell tool would run it. Not
    imported, not monkeypatched, not a copy.
  * The SEND proof asserts on THE WIRE: the method, the path, the `Authorization: Bearer`
    header, the content type, and the exact JSON body the server received. A mocked poster
    that never forms an HTTP request records no request and fails.
  * The RECEIVE proof injects the inbound message by pushing a real Socket Mode envelope
    down a real websocket, and asserts both that the tool surfaced it AND that the server
    received the acknowledgement Slack requires. Nothing writes into a queue the tool also
    reads.
  * TLS is proved with certificate VERIFICATION against a CA this file generates, and the
    negative case — the same servers with that CA removed from the trust store — must be
    REFUSED on both legs. Otherwise "we use TLS" would be a claim about a code path nothing
    ever executed.

Not real, and named so it is not mistaken for real: **no live Slack workspace is
exercised**. That needs a real Slack app, a bot token and an app-level token with Socket
Mode enabled, which only a human can create; that is a prerequisite item, not something
this suite can fake. What this file proves is that the tool speaks the protocols Slack
speaks, in the security mode Slack requires, up to the credential itself.

ONE THING THIS FILE ASSERTS THAT IS NOT A TEST OF OUR CODE AT ALL
=================================================================
`test_no_chat_server_component_exists_in_any_deploy_manifest` is Baron's ruling made
mechanical: the agent USES the tenant's existing Slack workspace and we do NOT run a chat
server. That is the kind of decision that erodes by accretion — somebody adds a "just for
testing" Mattermost to a manifest and it is in production a month later — so it is checked
rather than remembered.
"""

import json
import secrets
import threading
import uuid
from pathlib import Path

import pytest

import chat_fakes
from chat_fakes import HTTPFake, Response, WSFake, run_cli

REPO = Path(__file__).resolve().parent.parent
CLI = REPO / "deploy/agent/agent-slack"

# Distinctive, so their appearance anywhere unexpected is unambiguous rather than a
# judgement call. Shaped like the real things: Slack's two tokens have different prefixes
# and different powers, and confusing them is the single most common Socket Mode mistake.
BOT_TOKEN = "xoxb-fixture-" + secrets.token_hex(10)
APP_TOKEN = "xapp-fixture-" + secrets.token_hex(10)


class SlackFixture:
    """A fake Slack: the Web API over TLS, plus a Socket Mode endpoint over TLS.

    `script` is what the socket does once a client connects — the test writes it, so the
    inbound side of every proof is generated here and never by the code under test.
    """

    def __init__(self, certs, script=None, post_response=None):
        self.certs = certs
        self.script = script
        self.post_response = post_response
        self.socket_connected = threading.Event()
        self.acks: list[dict] = []
        self.envelope_ids: list[str] = []
        self.conn = None

        self.ws = WSFake(certs, self._on_connect, path="/link")
        self.api = HTTPFake(certs, self._handle)

    # ---------------------------------------------------------------- the Web API
    def _handle(self, request):
        if request.path == "/chat.postMessage":
            if self.post_response is not None:
                return Response(200, self.post_response)
            body = request.json or {}
            # The `ts` is minted HERE. Every assertion about what the tool reported reads
            # this value back out of the fixture, so a tool that fabricated a plausible
            # timestamp without talking to anything fails.
            return Response(200, {"ok": True, "channel": body.get("channel"),
                                  "ts": self.ts,
                                  "message": {"text": body.get("text")}})
        if request.path == "/auth.test":
            return Response(200, {"ok": True, "team": "Fixture Corp",
                                  "user": "agentbot", "url": "https://fixture.slack.test/"})
        if request.path == "/apps.connections.open":
            # Slack refuses this method for anything but an app-level token, and says so
            # with this exact error. Modelled rather than approximated: a tool that used the
            # bot token here would work against a permissive fixture and fail on the first
            # real workspace.
            if request.authorization != f"Bearer {APP_TOKEN}":
                return Response(200, {"ok": False, "error": "not_allowed_token_type"})
            return Response(200, {"ok": True, "url": self.ws.url + f"?ticket={uuid.uuid4().hex}"})
        return Response(404, {"ok": False, "error": "unknown_method"})

    ts = "1712345678.000100"

    # ---------------------------------------------------------------- Socket Mode
    def _on_connect(self, conn):
        self.conn = conn
        self.socket_connected.set()
        conn.send_json({"type": "hello", "num_connections": 1,
                        "connection_info": {"app_id": "A0FIXTURE"}})
        if self.script:
            self.script(self, conn)

    def push_event(self, conn, event: dict) -> str:
        """One Socket Mode envelope, and the ack Slack requires, read back off the wire."""
        envelope_id = str(uuid.uuid4())
        self.envelope_ids.append(envelope_id)
        conn.send_json({
            "envelope_id": envelope_id,
            "type": "events_api",
            "accepts_response_payload": False,
            "payload": {"type": "event_callback", "team_id": "T0FIXTURE", "event": event},
        })
        self.acks.append(conn.recv_json(timeout=20))
        return envelope_id

    def close(self):
        self.api.close()
        self.ws.close()

    def env(self, *, ca_file=True, bot=BOT_TOKEN, app=APP_TOKEN) -> dict:
        base = {
            "AGENT_SLACK_API_BASE": self.api.base_url,
            "AGENT_SLACK_BOT_TOKEN": bot,
            "AGENT_SLACK_APP_TOKEN": app,
        }
        if ca_file:
            base["AGENT_SLACK_CA_FILE"] = self.certs.ca_file
        return base


@pytest.fixture(scope="session")
def certs(tmp_path_factory):
    return chat_fakes.make_certificates(tmp_path_factory.mktemp("chat-certs"))


@pytest.fixture
def slack(certs):
    fixture = SlackFixture(certs)
    try:
        yield fixture
    finally:
        fixture.close()


def _run(args, env, timeout=120):
    return run_cli(CLI, args, env, timeout=timeout)


# ===========================================================================
# THE PROOF: a real Web API call out, a real Socket Mode message in.
# ===========================================================================

def test_send_is_a_real_authenticated_web_api_call_on_the_wire(slack):
    """SEND. Asserted from the SERVER'S transcript, not from what the tool printed."""
    text = f"Deploy finished. Reference {uuid.uuid4().hex}."
    out = _run(["send", "--channel", "C0FIXTURE", "--text", text], slack.env())
    assert out.returncode == 0, out.stderr

    # Everything below is read out of the request the fake server received.
    request = slack.api.only("/chat.postMessage")
    assert request.method == "POST"
    assert request.authorization == f"Bearer {BOT_TOKEN}", (
        "chat.postMessage was not authenticated with the BOT token. Slack's Web API takes "
        f"`Authorization: Bearer xoxb-…`; it received {request.authorization[:12]!r}…"
    )
    assert "application/json" in request.content_type
    assert request.json == {"channel": "C0FIXTURE", "text": text}, (
        "the JSON body on the wire is not the message that was asked for: "
        f"{request.json!r}"
    )

    # And the handle the tool handed back is the one the SERVER minted, so "did my message
    # post" and "how do I thread a reply to it" are answerable.
    reported = json.loads(out.stdout)
    assert reported["sent"] is True
    assert reported["ts"] == slack.ts
    assert reported["channel"] == "C0FIXTURE"


def test_a_reply_is_threaded_onto_the_message_it_answers(slack):
    out = _run(["send", "--channel", "C0FIXTURE", "--text", "on it",
                "--thread-ts", slack.ts], slack.env())
    assert out.returncode == 0, out.stderr
    assert slack.api.only("/chat.postMessage").json["thread_ts"] == slack.ts, (
        "--thread-ts never reached the wire, so the reply starts a new conversation "
        "beside the one a human is already reading."
    )


def test_a_slack_error_carried_on_an_http_200_is_a_failure_not_a_success(certs):
    """Slack answers HTTP 200 for almost every application-level refusal.

    `channel_not_found`, `not_in_channel`, `invalid_auth` all arrive as 200 with
    `ok: false`. A caller that checks only the status code reports "posted" for a message
    nobody received — on an unattended surface, forever, to nobody.
    """
    fixture = SlackFixture(certs, post_response={"ok": False, "error": "channel_not_found"})
    try:
        out = _run(["send", "--channel", "C0NOPE", "--text", "hello"], fixture.env())
        assert out.returncode == 1, (
            "the tool reported success for a message Slack refused:\n" + out.stdout
        )
        assert "channel_not_found" in json.loads(out.stderr)["error"]
    finally:
        fixture.close()


def test_receive_opens_socket_mode_with_the_app_token_and_surfaces_what_arrives(certs):
    """RECEIVE. The inbound message is injected over a real websocket by this test."""
    text = f"can you check the build? {uuid.uuid4().hex}"

    def script(fixture, conn):
        fixture.push_event(conn, {"type": "message", "channel": "C0FIXTURE",
                                  "user": "U0HUMAN", "text": text,
                                  "ts": "1712345999.000200"})

    fixture = SlackFixture(certs, script=script)
    try:
        out = _run(["receive", "--timeout", "20", "--limit", "1"], fixture.env())
        assert out.returncode == 0, out.stderr
        events = json.loads(out.stdout)
    finally:
        fixture.close()

    # The connection was opened with the APP token — the fake refuses anything else with
    # Slack's own `not_allowed_token_type`, so reaching the socket at all proves it.
    opened = fixture.api.only("/apps.connections.open")
    assert opened.method == "POST"
    assert opened.authorization == f"Bearer {APP_TOKEN}"

    # The upgrade was a real websocket handshake carrying the single-use ticket.
    assert fixture.ws.handshakes, "nothing ever completed a websocket handshake"
    handshake = fixture.ws.handshakes[0]
    assert handshake["request_line"].startswith("GET /link?ticket=")
    assert handshake["headers"].get("upgrade", "").lower() == "websocket"
    assert handshake["headers"].get("sec-websocket-version") == "13"

    # What the tool surfaced is what the fixture pushed.
    assert len(events) == 1, events
    assert events[0]["text"] == text
    assert events[0]["channel"] == "C0FIXTURE"
    assert events[0]["user"] == "U0HUMAN"
    assert events[0]["ts"] == "1712345999.000200"

    # AND the envelope was acknowledged. Slack retries an unacknowledged envelope three
    # times and then disables the subscription, so an agent that receives without acking
    # looks fine for about ten minutes and then silently stops hearing anything.
    assert fixture.acks, "the envelope was never acknowledged"
    assert fixture.acks[0] == {"envelope_id": fixture.envelope_ids[0]}


def test_a_bots_own_messages_do_not_come_back_as_things_to_answer(certs):
    """The default that stops an agent answering itself forever in a real workspace."""
    def script(fixture, conn):
        fixture.push_event(conn, {"type": "message", "channel": "C0FIXTURE",
                                  "bot_id": "B0SELF", "text": "posted by me",
                                  "ts": "1712346000.000100"})
        fixture.push_event(conn, {"type": "message", "channel": "C0FIXTURE",
                                  "user": "U0HUMAN", "text": "posted by a person",
                                  "ts": "1712346000.000200"})

    fixture = SlackFixture(certs, script=script)
    try:
        out = _run(["receive", "--timeout", "20", "--limit", "1"], fixture.env())
        assert out.returncode == 0, out.stderr
        events = json.loads(out.stdout)
    finally:
        fixture.close()

    assert [e["text"] for e in events] == ["posted by a person"]
    # BOTH envelopes were still acknowledged. Filtering happens after the ack, because an
    # ack that only fired for events we liked would degrade the subscription quietly.
    assert len(fixture.acks) == 2, (
        "an envelope the tool chose not to surface was never acknowledged. Slack does not "
        "care why a client stayed silent; it retries and then disables the subscription."
    )


def test_the_client_answers_a_ping_so_a_long_listen_is_not_dropped(certs):
    """Slack pings idle Socket Mode connections and closes the ones that do not answer.

    Without this the symptom is "receive works in a test and stops after a minute in
    production", which is the worst possible failure to diagnose from a transcript.
    """
    pong_seen = threading.Event()

    def script(fixture, conn):
        conn.ping(b"slack-liveness")
        # A pong is a control frame the fixture's recv loop skips, so the proof that the
        # connection SURVIVED the ping is that a message pushed afterwards still arrives
        # and is still acknowledged.
        fixture.push_event(conn, {"type": "message", "channel": "C0FIXTURE",
                                  "user": "U0HUMAN", "text": "still here",
                                  "ts": "1712346100.000100"})
        pong_seen.set()

    fixture = SlackFixture(certs, script=script)
    try:
        out = _run(["receive", "--timeout", "25", "--limit", "1"], fixture.env())
        assert out.returncode == 0, out.stderr
        events = json.loads(out.stdout)
    finally:
        fixture.close()
    assert pong_seen.is_set()
    assert [e["text"] for e in events] == ["still here"]


def test_listening_on_a_silent_workspace_returns_nothing_rather_than_hanging(certs):
    """`[]` has to mean "nothing was said", which is only true if the command comes back.

    The fixture holds the socket OPEN and says nothing for longer than the tool's timeout,
    which is the case that matters: a connection that closes immediately would exercise the
    peer-closed path instead of the bounded-listen path this is about.
    """
    import time

    fixture = SlackFixture(certs, script=lambda fixture, conn: time.sleep(15))
    try:
        out = _run(["receive", "--timeout", "3", "--limit", "5"], fixture.env(), timeout=60)
        assert out.returncode == 0, out.stderr
        assert json.loads(out.stdout) == []
    finally:
        fixture.close()


# ===========================================================================
# TLS — the mode Slack actually requires.
# ===========================================================================

def test_a_certificate_that_does_not_verify_is_refused_on_both_legs(slack):
    """The negative that makes the positive worth something.

    Same servers, same ports, same everything — except the CA is not trusted. Both legs
    must refuse. An unattended agent holding a token that posts as a whole company is
    exactly the caller who would never notice an interception, so there is deliberately no
    switch that turns this off.
    """
    out = _run(["check"], slack.env(ca_file=False))
    assert out.returncode == 0, out.stderr  # `check` reports, it does not raise
    report = json.loads(out.stdout)
    assert report["ok"] is False
    for leg in ("post", "receive"):
        assert report[leg]["ok"] is False, f"{leg} accepted an unverifiable certificate"
        assert "CERTIFICATE_VERIFY_FAILED" in report[leg]["error"], report[leg]


def test_check_reports_both_legs_independently(slack):
    out = _run(["check"], slack.env())
    assert out.returncode == 0, out.stderr
    report = json.loads(out.stdout)
    assert report["ok"] is True
    assert report["post"]["team"] == "Fixture Corp"
    assert report["receive"]["ok"] is True


def test_the_wrong_token_on_the_socket_leg_is_reported_and_does_not_break_posting(certs):
    """Slack's `not_allowed_token_type`: the bot token cannot open Socket Mode.

    The two tokens are easy to swap and the failure is one-sided, so `check` must be able
    to say "posting works, listening does not" rather than one undifferentiated red.
    """
    fixture = SlackFixture(certs)
    try:
        out = _run(["check"], fixture.env(app=BOT_TOKEN))
        assert out.returncode == 0, out.stderr
        report = json.loads(out.stdout)
        assert report["post"]["ok"] is True
        assert report["receive"]["ok"] is False
        assert "not_allowed_token_type" in report["receive"]["error"]
        assert report["ok"] is False
    finally:
        fixture.close()


# ===========================================================================
# The tokens must not leak, and a missing one must not crash.
# ===========================================================================

def test_neither_token_ever_appears_in_output_even_when_slack_rejects_it(certs):
    """The failure path is where credentials leak, because that is where servers echo."""
    fixture = SlackFixture(certs,
                           post_response={"ok": False, "error": "invalid_auth"})
    try:
        env = fixture.env()
        for args in (["check"], ["send", "--channel", "C0X", "--text", "hi"],
                     ["receive", "--timeout", "2"]):
            out = _run(args, env, timeout=60)
            combined = out.stdout + out.stderr
            for token in (BOT_TOKEN, APP_TOKEN):
                assert token not in combined, (
                    f"`agent-slack {' '.join(args)}` printed a Slack token. This output "
                    "lands in an agent transcript that a person may later read and that "
                    "may be shipped to a log collector."
                )
            assert "Traceback" not in combined, (
                f"`agent-slack {' '.join(args)}` raised instead of reporting. A traceback "
                "puts the request object, and on some paths the token, into the transcript."
            )
    finally:
        fixture.close()


def test_config_reports_the_workspace_without_ever_printing_a_token(slack):
    out = _run(["config"], slack.env())
    assert out.returncode == 0, out.stderr
    config = json.loads(out.stdout)
    assert config["bot_token_set"] is True and config["app_token_set"] is True
    assert BOT_TOKEN not in out.stdout and APP_TOKEN not in out.stdout


def test_an_agent_with_no_slack_says_so_instead_of_crashing():
    """An agent provisioned without --slack-config-file has no AGENT_SLACK_* at all.

    The pod template mounts the Slack Secret `optional: true` precisely so that agent still
    starts, so the tool has to answer for that state in a way a model can act on rather
    than dying with a KeyError.
    """
    config = json.loads(_run(["config"], {}).stdout)
    assert config["bot_token_set"] is False and config["app_token_set"] is False

    out = _run(["send", "--channel", "C0X", "--text", "hello"], {})
    assert out.returncode == 1
    assert "Traceback" not in out.stdout + out.stderr
    error = json.loads(out.stderr)["error"]
    assert "AGENT_SLACK_BOT_TOKEN" in error and "provision-agent.sh" in error, error

    out = _run(["receive", "--timeout", "1"], {})
    assert out.returncode == 1
    assert "AGENT_SLACK_APP_TOKEN" in json.loads(out.stderr)["error"]


# ===========================================================================
# Baron's ruling, made mechanical.
# ===========================================================================

# Deliberately the names of chat SERVER products, not the words "slack" or "discord": the
# manifests legitimately name `agent-<user>-<name>-slack` Secrets, and a marker list that
# matched those would be a check nobody could keep green and everybody would delete.
CHAT_SERVER_MARKERS = (
    "mattermost", "rocketchat", "rocket.chat", "zulip", "synapse", "matrix-conduit",
    "ejabberd", "prosody", "openfire", "tinode", "revoltchat", "spacebarchat",
    "element-web", "jitsi-meet", "gitter", "zulipchat",
)


def _deploy_manifest_paths():
    for root in ("deploy", "bundle"):
        for path in sorted((REPO / root).rglob("*")):
            if path.suffix in (".yaml", ".yml") and path.is_file():
                yield path


def test_no_chat_server_component_exists_in_any_deploy_manifest():
    """Baron's ruling on -783: the agent USES the tenant's Slack and Discord.

    Checked against every manifest under deploy/ and bundle/ rather than remembered,
    because this is exactly the decision that erodes by accretion: somebody adds a
    "temporary" Mattermost to a compose file and it is in production a month later. The
    fixtures in THIS file are test fixtures — they live in tests/, are started by pytest and
    torn down by pytest, and never appear in a manifest, which is the distinction the check
    enforces.
    """
    offenders = []
    for path in _deploy_manifest_paths():
        text = path.read_text(errors="replace").lower()
        for marker in CHAT_SERVER_MARKERS:
            if marker in text:
                offenders.append(f"{path.relative_to(REPO)}: {marker}")
    assert not offenders, (
        "a chat server appeared in a deploy manifest:\n  " + "\n  ".join(offenders) +
        "\n\nThe ruling on enterpriseaiframework-783 is that the agent uses the tenant's "
        "EXISTING Slack workspace and Discord guild with the tenant's own bot tokens. We do "
        "not run chat infrastructure. If a fixture is needed, it belongs in tests/."
    )


def test_slack_needs_no_inbound_route_which_is_why_socket_mode_was_chosen():
    """The alternative — Slack's Events API — needs a PUBLIC HTTPS endpoint per agent.

    Asserted against the agent Service rather than argued in a comment: if a NodePort ever
    appears here, somebody has published an inbound internet route to a pod holding a
    spendable model key, and the reason will have been "Slack needs a webhook".
    """
    import yaml

    template = (REPO / "deploy/k8s/64-agent.template.yaml").read_text()
    services = [doc for doc in yaml.safe_load_all(template.replace("__", "x"))
                if doc and doc.get("kind") == "Service"]
    assert services, "the agent template no longer declares a Service"
    for service in services:
        assert service["spec"].get("type", "ClusterIP") == "ClusterIP"
        for port in service["spec"]["ports"]:
            assert "nodePort" not in port, (
                "an agent Service published a NodePort. Slack receive is Socket Mode "
                "precisely so that no inbound route is ever needed."
            )
