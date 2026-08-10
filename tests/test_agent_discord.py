"""The resident agent can post to and receive from Discord (enterpriseaiframework-783).

The structure, and the reasoning behind it, is tests/test_agent_slack.py's — read that
file's header for what is real here and what is not. The short form: a TLS HTTP server and
a TLS websocket server that this file owns speak real HTTP/1.1 and real RFC 6455; the
subject is `deploy/agent/agent-discord`, the actual file the pod puts on PATH, run as a
subprocess; every expected value comes out of the server's transcript rather than out of
the code under test; and no live Discord application is exercised, because that needs a
real bot token only a human can create.

WHAT DIFFERS FROM SLACK, AND WHY IT NEEDED ITS OWN PROOF
=======================================================
  * ONE token does both directions. `Authorization: Bot <token>` — with the literal `Bot `
    prefix, which Discord requires and without which the same correct token is a 401.
  * The Gateway is a HANDSHAKE, not a subscription: HELLO, then the client must IDENTIFY
    with the token AND an intents bitfield, then READY, then events. A client that skipped
    IDENTIFY would connect and hear nothing, forever, with no error.
  * INTENTS are the trap. Without MESSAGE_CONTENT (1<<15) Discord delivers every message
    with an EMPTY `content` and reports nothing wrong at all. The tool has to ask for it and
    has to say so when messages arrive blank, and both are asserted here.
  * HEARTBEATS. The Gateway closes a connection that misses its interval, so a long listen
    that does not beat "works in a test and stops after a minute in production".
"""

import json
import secrets
import threading
import time
import uuid
from pathlib import Path

import pytest

import chat_fakes
from chat_fakes import HTTPFake, Response, WSFake, run_cli

REPO = Path(__file__).resolve().parent.parent
CLI = REPO / "deploy/agent/agent-discord"

BOT_TOKEN = "fixture.discord." + secrets.token_hex(12)

# GUILD_MESSAGES | DIRECT_MESSAGES | MESSAGE_CONTENT. Written out here from the bit
# positions in Discord's documentation rather than imported from the tool, so a tool that
# quietly dropped MESSAGE_CONTENT fails this instead of agreeing with itself.
EXPECTED_INTENTS = (1 << 9) | (1 << 12) | (1 << 15)


class DiscordFixture:
    """A fake Discord: the REST API over TLS, plus a Gateway websocket over TLS."""

    MESSAGE_ID = "1122334455667788990"

    def __init__(self, certs, script=None, post_status=200, post_body=None):
        self.certs = certs
        self.script = script
        self.post_status = post_status
        self.post_body = post_body
        self.identify = None
        self.heartbeats: list[dict] = []
        self.identified = threading.Event()

        self.ws = WSFake(certs, self._on_connect, path="/gateway")
        self.api = HTTPFake(certs, self._handle)

    # ---------------------------------------------------------------- the REST API
    def _handle(self, request):
        # Every route requires `Authorization: Bot <token>`, exactly as Discord does. A
        # missing `Bot ` prefix is a 401 there and a 401 here, so the tool cannot pass by
        # sending a bare token.
        if request.authorization != f"Bot {BOT_TOKEN}":
            return Response(401, {"message": "401: Unauthorized", "code": 0})
        if request.path.endswith("/messages") and request.method == "POST":
            if self.post_body is not None or self.post_status != 200:
                return Response(self.post_status,
                                self.post_body if self.post_body is not None
                                else {"message": "Missing Access", "code": 50001})
            body = request.json or {}
            channel = request.path.split("/")[-2]
            # The message id is minted HERE, so a tool that fabricated one without talking
            # to anything fails.
            return Response(200, {"id": self.MESSAGE_ID, "channel_id": channel,
                                  "content": body.get("content")})
        if request.path.startswith("/api/v10/gateway/bot"):
            return Response(200, {"url": f"wss://{chat_fakes.FAKE_HOST}:{self.ws.port}/gateway",
                                  "shards": 1,
                                  "session_start_limit": {"total": 1000, "remaining": 999}})
        if request.path.startswith("/api/v10/users/@me"):
            return Response(200, {"id": "999000111", "username": "fixture-bot", "bot": True})
        return Response(404, {"message": "404: Not Found", "code": 0})

    # ---------------------------------------------------------------- the Gateway
    def _on_connect(self, conn):
        conn.send_json({"op": 10, "d": {"heartbeat_interval": 1000}})
        self.identify = conn.recv_json(timeout=20)
        self.identified.set()
        conn.send_json({"op": 0, "s": 1, "t": "READY",
                        "d": {"session_id": "abc", "user": {"id": "999000111",
                                                            "username": "fixture-bot"}}})
        if self.script:
            self.script(self, conn)

    def push_message(self, conn, *, content: str, author_bot: bool = False,
                     channel: str = "555000111", sequence: int = 2) -> None:
        conn.send_json({
            "op": 0, "s": sequence, "t": "MESSAGE_CREATE",
            "d": {"id": str(uuid.uuid4().int)[:19], "channel_id": channel,
                  "guild_id": "777000222", "content": content,
                  "timestamp": "2026-08-10T12:00:00.000000+00:00",
                  "author": {"id": "444000333", "username": "a-person", "bot": author_bot}},
        })

    def close(self):
        self.api.close()
        self.ws.close()

    def env(self, *, ca_file=True, token=BOT_TOKEN, extra=None) -> dict:
        base = {
            "AGENT_DISCORD_API_BASE": self.api.base_url,
            "AGENT_DISCORD_BOT_TOKEN": token,
        }
        if ca_file:
            base["AGENT_DISCORD_CA_FILE"] = self.certs.ca_file
        base.update(extra or {})
        return base


@pytest.fixture(scope="session")
def certs(tmp_path_factory):
    return chat_fakes.make_certificates(tmp_path_factory.mktemp("discord-certs"))


@pytest.fixture
def discord(certs):
    fixture = DiscordFixture(certs)
    try:
        yield fixture
    finally:
        fixture.close()


def _run(args, env, timeout=120):
    return run_cli(CLI, args, env, timeout=timeout)


# ===========================================================================
# THE PROOF: a real REST call out, a real Gateway message in.
# ===========================================================================

def test_send_is_a_real_authenticated_rest_call_on_the_wire(discord):
    """SEND. Asserted from the SERVER'S transcript, not from what the tool printed."""
    text = f"Build 4711 is green. {uuid.uuid4().hex}"
    out = _run(["send", "--channel", "555000111", "--text", text], discord.env())
    assert out.returncode == 0, out.stderr

    request = discord.api.only("/api/v10/channels/555000111/messages")
    assert request.method == "POST"
    assert request.authorization == f"Bot {BOT_TOKEN}", (
        "the REST call was not authenticated as a BOT. Discord requires the literal `Bot `"
        "prefix; without it the same correct token is a 401 that looks like a bad token."
    )
    assert "application/json" in request.content_type
    body = request.json
    assert body["content"] == text
    # Mentions off by default, on the wire. A summary containing the string `@everyone`
    # must not notify a whole company's server.
    assert body["allowed_mentions"] == {"parse": []}, (
        f"mentions were not suppressed on the wire: {body!r}"
    )

    reported = json.loads(out.stdout)
    assert reported["sent"] is True
    assert reported["message_id"] == discord.MESSAGE_ID


def test_mentions_are_only_live_when_they_were_asked_for(discord):
    out = _run(["send", "--channel", "555000111", "--text", "@everyone deploy done",
                "--allow-mentions"], discord.env())
    assert out.returncode == 0, out.stderr
    assert "allowed_mentions" not in discord.api.only(
        "/api/v10/channels/555000111/messages").json


def test_a_reply_references_the_message_it_answers(discord):
    out = _run(["send", "--channel", "555000111", "--text", "on it",
                "--reply-to", "998877"], discord.env())
    assert out.returncode == 0, out.stderr
    body = discord.api.only("/api/v10/channels/555000111/messages").json
    assert body["message_reference"] == {"message_id": "998877"}


def test_an_http_error_from_discord_is_a_failure_not_a_success(certs):
    fixture = DiscordFixture(certs, post_status=403,
                             post_body={"message": "Missing Access", "code": 50001})
    try:
        out = _run(["send", "--channel", "555000111", "--text", "hello"], fixture.env())
        assert out.returncode == 1, (
            "the tool reported success for a message Discord refused:\n" + out.stdout
        )
        error = json.loads(out.stderr)["error"]
        assert "403" in error and "Missing Access" in error
    finally:
        fixture.close()


def test_a_rate_limit_is_reported_as_a_rate_limit(certs):
    """429 is the one failure an unattended poster will actually hit, and it is not a bug.

    Reported by name so whoever reads the transcript slows down rather than concluding the
    token is broken.
    """
    fixture = DiscordFixture(certs, post_status=429,
                             post_body={"message": "You are being rate limited.",
                                        "retry_after": 4.2, "global": False})
    try:
        out = _run(["send", "--channel", "555000111", "--text", "hello"], fixture.env())
        assert out.returncode == 1
        assert "rate-limited" in json.loads(out.stderr)["error"]
    finally:
        fixture.close()


def test_receive_identifies_on_the_gateway_and_surfaces_what_arrives(certs):
    """RECEIVE. The inbound message is injected over a real websocket by this test."""
    content = f"can somebody look at the queue? {uuid.uuid4().hex}"

    def script(fixture, conn):
        fixture.push_message(conn, content=content)
        time.sleep(2)  # hold the socket open so the tool's own bound is what ends it

    fixture = DiscordFixture(certs, script=script)
    try:
        out = _run(["receive", "--timeout", "20", "--limit", "1"], fixture.env())
        assert out.returncode == 0, out.stderr
        result = json.loads(out.stdout)
    finally:
        fixture.close()

    # The Gateway url was fetched with the bot's own credential — /gateway/bot is the
    # authenticated variant, so reaching the socket at all proves the REST leg too.
    assert any(r.path.startswith("/api/v10/gateway/bot") for r in fixture.api.requests)

    handshake = fixture.ws.handshakes[0]
    assert handshake["request_line"].startswith("GET /gateway?v=10&encoding=json"), (
        "the Gateway was opened without the pinned API version and json encoding: "
        f"{handshake['request_line']}"
    )
    assert handshake["headers"].get("upgrade", "").lower() == "websocket"

    # IDENTIFY, read off the wire. This is the frame a client that "connected" but never
    # identified would not have sent, and its absence is invisible from the client side.
    assert fixture.identify is not None, "the client never sent an IDENTIFY"
    assert fixture.identify["op"] == 2
    assert fixture.identify["d"]["token"] == BOT_TOKEN
    assert fixture.identify["d"]["intents"] == EXPECTED_INTENTS, (
        "the IDENTIFY did not ask for the intents this tool needs. Without MESSAGE_CONTENT "
        "Discord delivers every message with empty content and reports no error at all."
    )
    assert "properties" in fixture.identify["d"]

    assert result["connected"] is True
    assert result["identity"]["bot_user"] == "fixture-bot"
    assert [m["content"] for m in result["messages"]] == [content]
    assert result["messages"][0]["channel"] == "555000111"
    assert result["messages"][0]["author"] == "a-person"


def test_the_intents_bitfield_is_configurable_and_reaches_the_wire(certs):
    """A tenant that will not approve MESSAGE_CONTENT must be able to run without it — and
    the tool must then say so rather than quietly returning blank messages."""
    guild_messages_only = str(1 << 9)

    def script(fixture, conn):
        fixture.push_message(conn, content="")
        time.sleep(2)

    fixture = DiscordFixture(certs, script=script)
    try:
        out = _run(["receive", "--timeout", "20", "--limit", "1"],
                   fixture.env(extra={"AGENT_DISCORD_INTENTS": guild_messages_only}))
        assert out.returncode == 0, out.stderr
        result = json.loads(out.stdout)
    finally:
        fixture.close()

    assert fixture.identify["d"]["intents"] == int(guild_messages_only)
    assert result["message_content_intent"] is False
    assert "warning" in result and "MESSAGE CONTENT" in result["warning"], (
        "every message arrived blank and the tool said nothing. That is exactly what "
        "Discord does when the intent is off, and there is no other signal anywhere."
    )


def test_a_bots_own_messages_do_not_come_back_as_things_to_answer(certs):
    def script(fixture, conn):
        fixture.push_message(conn, content="posted by me", author_bot=True, sequence=2)
        fixture.push_message(conn, content="posted by a person", sequence=3)
        time.sleep(2)

    fixture = DiscordFixture(certs, script=script)
    try:
        out = _run(["receive", "--timeout", "20", "--limit", "1"], fixture.env())
        assert out.returncode == 0, out.stderr
        result = json.loads(out.stdout)
    finally:
        fixture.close()
    assert [m["content"] for m in result["messages"]] == ["posted by a person"]


def test_the_client_heartbeats_so_a_long_listen_is_not_dropped(certs):
    """The Gateway closes a connection that misses its heartbeat interval.

    The fixture advertises a 1-second interval and then says nothing, so a client that does
    not beat produces no op 1 at all. Read off the wire, because a missing heartbeat is
    invisible from the client's own side until the connection dies.
    """
    beats: list[dict] = []

    def script(fixture, conn):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                frame = conn.recv_json(timeout=2)
            except Exception:
                break
            if frame.get("op") == 1:
                beats.append(frame)
                conn.send_json({"op": 11})
        fixture.push_message(conn, content="still here", sequence=9)
        time.sleep(1)

    fixture = DiscordFixture(certs, script=script)
    try:
        out = _run(["receive", "--timeout", "12", "--limit", "1"], fixture.env())
        assert out.returncode == 0, out.stderr
        result = json.loads(out.stdout)
    finally:
        fixture.close()

    assert beats, (
        "the client never sent a heartbeat. Discord closes the connection at the first "
        "missed interval, which presents as 'receive stops working after a minute'."
    )
    assert beats[0]["op"] == 1
    assert [m["content"] for m in result["messages"]] == ["still here"]


def test_listening_on_a_silent_guild_returns_nothing_rather_than_hanging(certs):
    fixture = DiscordFixture(certs, script=lambda fixture, conn: time.sleep(15))
    try:
        out = _run(["receive", "--timeout", "3", "--limit", "5"], fixture.env(), timeout=60)
        assert out.returncode == 0, out.stderr
        result = json.loads(out.stdout)
        assert result["messages"] == []
        assert result["connected"] is True
    finally:
        fixture.close()


# ===========================================================================
# TLS — the mode Discord actually requires.
# ===========================================================================

def test_a_certificate_that_does_not_verify_is_refused_on_both_legs(discord):
    out = _run(["check"], discord.env(ca_file=False))
    assert out.returncode == 0, out.stderr
    report = json.loads(out.stdout)
    assert report["ok"] is False
    for leg in ("post", "receive"):
        assert report[leg]["ok"] is False, f"{leg} accepted an unverifiable certificate"
        assert "CERTIFICATE_VERIFY_FAILED" in report[leg]["error"], report[leg]


def test_check_reports_both_legs_independently(discord):
    out = _run(["check"], discord.env())
    assert out.returncode == 0, out.stderr
    report = json.loads(out.stdout)
    assert report["ok"] is True
    assert report["post"]["bot_user"] == "fixture-bot"
    assert report["receive"]["session_starts_left"] == 999


# ===========================================================================
# The token must not leak, and a missing one must not crash.
# ===========================================================================

def test_the_token_never_appears_in_output_even_when_discord_rejects_it(discord):
    env = discord.env(token="wrong-" + secrets.token_hex(8))
    wrong = env["AGENT_DISCORD_BOT_TOKEN"]
    for args in (["check"], ["send", "--channel", "555000111", "--text", "hi"],
                 ["receive", "--timeout", "2"]):
        out = _run(args, env, timeout=60)
        combined = out.stdout + out.stderr
        assert wrong not in combined, (
            f"`agent-discord {' '.join(args)}` printed the bot token. This output lands in "
            "an agent transcript that a person may later read."
        )
        assert "Traceback" not in combined, (
            f"`agent-discord {' '.join(args)}` raised instead of reporting."
        )


def test_config_reports_the_bot_without_ever_printing_the_token(discord):
    out = _run(["config"], discord.env())
    assert out.returncode == 0, out.stderr
    config = json.loads(out.stdout)
    assert config["bot_token_set"] is True
    assert config["message_content_intent"] is True
    assert BOT_TOKEN not in out.stdout


def test_an_agent_with_no_discord_says_so_instead_of_crashing():
    """The Discord Secret is mounted `optional: true`, so this state is normal."""
    config = json.loads(_run(["config"], {}).stdout)
    assert config["bot_token_set"] is False

    out = _run(["send", "--channel", "1", "--text", "hello"], {})
    assert out.returncode == 1
    assert "Traceback" not in out.stdout + out.stderr
    error = json.loads(out.stderr)["error"]
    assert "AGENT_DISCORD_BOT_TOKEN" in error and "provision-agent.sh" in error, error


# ===========================================================================
# Baron's ruling, made mechanical — the Discord half.
# ===========================================================================

def test_no_self_hosted_discord_alike_appears_in_any_deploy_manifest():
    """The same ruling tests/test_agent_slack.py asserts, from the other direction.

    Kept as its own check rather than folded into that file's marker list because the
    tempting components differ: nobody adds Mattermost to get Discord, they add Revolt or
    Spacebar, and a list that only names the Slack-alikes would go quiet on exactly the
    substitution this connector invites.
    """
    markers = ("revolt", "spacebar", "fosscord", "guilded-server", "concord-server")
    offenders = []
    for root in ("deploy", "bundle"):
        for path in sorted((REPO / root).rglob("*")):
            if path.suffix not in (".yaml", ".yml") or not path.is_file():
                continue
            text = path.read_text(errors="replace").lower()
            offenders += [f"{path.relative_to(REPO)}: {m}" for m in markers if m in text]
    assert not offenders, (
        "a self-hosted Discord-alike appeared in a deploy manifest:\n  "
        + "\n  ".join(offenders) +
        "\n\nThe ruling on enterpriseaiframework-783 is that the agent uses the tenant's "
        "EXISTING Discord guild with the tenant's own bot token. We do not run chat "
        "infrastructure."
    )
