"""enterpriseaiframework-f50: the chat surface runs a LibreChat that has Agent Skills.

Skills shipped in LibreChat v0.8.6 and the self-hosted Apache-2.0 code interpreter in
v0.8.7; the bundle and the cluster were both pinned to v0.8.0, so three p0 features were
blocked on a version rather than on a design.

WHAT THIS FILE IS FOR — and it is not "the tag says 0.8.7". An upgrade of a configurable
component fails in exactly one interesting way: the surface comes up healthy with a
configuration it quietly stopped honouring. That is not hypothetical here. Booting v0.8.7
against the v0.8.0 configuration produced, with a 200 from /health throughout:

  * `[memory] Agent config detected without explicit `enabled: true`. Automatic memory
    extraction is now opt-in.` — memory extraction silently off. The memories panel still
    renders and /api/memories still answers; conversations simply stop learning anything.
  * `[MCP][echo] Failed to initialize: Domain "http://mcp-echo:8080" is not allowed` /
    `[MCP] Initialized with 1 configured server and 0 tools.` — v0.8.6+ refuses MCP
    servers that resolve into private IP space unless exempted. One configured server,
    zero tools, no error the user ever sees.

Both were found by reading the container's own boot log, and neither is in the changelog.
So the tests below assert the *effects* of librechat.yaml against the running surface —
the tool our MCP server exposes, the specs the client is served, the endpoints on offer —
rather than asserting that the file still contains the lines we wrote in it.

The failure mode this whole file guards is the one v0.8.0 has: when
`configSchema.strict().safeParse` rejects librechat.yaml, v0.8.0's loadCustomConfig logs
and `return null`, and the surface starts with NO custom config at all — no gateway
endpoint, no modelSpecs, no memory, no MCP — and still answers /health with 200. v0.8.7
calls `process.exit(1)` instead, which is a real improvement, but the bundle and the
cluster share one librechat.yaml (deploy/bin/deploy.sh renders this exact file into the
cluster's chat-config ConfigMap), so the image pin and the config must move together.
"""

import re
import subprocess
import time
import uuid
from pathlib import Path

import httpx
import pytest
import yaml

import chat_turn
import oidc_login
from conftest import BUNDLE, DOGFOOD_USER, DOGFOOD_PASSWORD

pytestmark = pytest.mark.usefixtures("stack_up")

TIMEOUT = 60.0

REPO = Path(__file__).resolve().parent.parent
COMPOSE_FILE = BUNDLE / "docker-compose.yml"
K8S_CHAT_FILE = REPO / "deploy" / "k8s" / "50-chat.yaml"
LIBRECHAT_YAML = BUNDLE / "librechat" / "librechat.yaml"

IMAGE_RE = re.compile(r"ghcr\.io/danny-avila/librechat:(v[0-9][^\s\"']*)")

# The release that introduced Agent Skills and subagents. Anything older cannot serve
# enterpriseaiframework-6ff (skills in chat) or 082 (code execution) no matter how it is
# configured.
MINIMUM_VERSION = (0, 8, 6)


def _pinned_versions(path: Path) -> list[str]:
    return IMAGE_RE.findall(path.read_text())


def _parse(tag: str) -> tuple[int, ...]:
    return tuple(int(part) for part in tag.lstrip("v").split("-")[0].split("."))


@pytest.fixture(scope="module")
def librechat_config() -> dict:
    return yaml.safe_load(LIBRECHAT_YAML.read_text())


@pytest.fixture(scope="module")
def chat_container(env) -> str:
    """The name of THIS bundle's chat container.

    Derived from the compose project name rather than hardcoded: a linked worktree runs
    its own isolated stack (enterpriseaiframework-0e3), and a hardcoded
    `enterprise-ai-chat-1` would inspect the PRIMARY checkout's container — reporting a
    green result about a stack this test never touched.
    """
    project = env.get("COMPOSE_PROJECT_NAME", "enterprise-ai")
    name = f"{project}-chat-1"
    listed = subprocess.run(
        ["docker", "ps", "--filter", f"name=^{name}$", "--format", "{{.Names}}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert listed == name, (
        f"chat container {name!r} is not running (docker ps returned {listed!r}); "
        "these tests assert against the surface this bundle actually started"
    )
    return name


@pytest.fixture(scope="module")
def startup_config(chat_session, chat_url) -> dict:
    """GET /api/config — what the browser is told the surface can do."""
    token = _token(chat_session, chat_url)
    r = chat_session.get(
        f"{chat_url}/api/config",
        headers={
            "Authorization": f"Bearer {token}",
            "Cookie": oidc_login._cookie_header(chat_session),
        },
        timeout=TIMEOUT,
    )
    assert r.status_code == 200, r.text
    return r.json()


def _token(client, chat_url: str) -> str:
    r = client.post(
        f"{chat_url}/api/auth/refresh",
        headers={"Cookie": oidc_login._cookie_header(client)},
        timeout=TIMEOUT,
    )
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _auth(client, chat_url: str) -> dict:
    return {
        "Authorization": f"Bearer {_token(client, chat_url)}",
        "Cookie": oidc_login._cookie_header(client),
    }


def _logs_since(container: str, since: float) -> str:
    """Container log from an exact instant.

    `--since 5m` is not good enough and the difference is not academic: with a fixed
    window, a line emitted by an EARLIER test (or by the boot before a config change) is
    still inside it, so a test meant to prove "this turn caused that line" passes on
    evidence from a different turn. That happened while building this file — the
    memory-agent assertion below passed with memory extraction disabled, reading a line
    from before the container was restarted. Anchoring to a timestamp taken immediately
    before the turn is what makes the assertion mean what it says.
    """
    result = subprocess.run(
        # int() TRUNCATES; f"{since:.0f}" ROUNDS. Rounding a fractional timestamp up moves
        # the window's start into the FUTURE by up to half a second, so a line the turn
        # emitted inside that gap is invisible and the poll times out — intermittently,
        # depending only on the fractional part of the clock when the test began, which is
        # why this failed under load and passed in isolation (enterpriseaiframework-c8d).
        # Truncating widens the window backwards by under a second instead, which is safe:
        # `since` is captured immediately before the turn, so nothing else of ours is in it.
        ["docker", "logs", "--since", str(int(since)), container],
        capture_output=True, text=True, check=True,
    )
    return result.stdout + result.stderr


def _wait_for_log(container: str, since: float, needle: str, timeout: float = 60.0) -> str:
    """Poll the log until `needle` appears, and return everything logged since `since`.

    Titling and memory extraction are side effects the surface performs AFTER the
    assistant message is persisted, so reading the log the instant the reply lands is a
    race — the work has not happened yet. Polling is the difference between a test that
    proves the surface did something and one that flakes on how fast the machine is.
    """
    deadline = time.monotonic() + timeout
    combined = ""
    while time.monotonic() < deadline:
        combined = _logs_since(container, since)
        if needle in combined:
            return combined
        time.sleep(2)
    return combined


# ---------------------------------------------------------------------------
# The pin itself, and the drift between the two deployments (rd 634)
# ---------------------------------------------------------------------------

class TestTheVersionPin:
    def test_the_pinned_version_has_agent_skills(self):
        """Fails on the tree this item was picked up from: both files said v0.8.0."""
        tags = _pinned_versions(COMPOSE_FILE)
        assert tags, f"no LibreChat image pin found in {COMPOSE_FILE}"
        for tag in tags:
            assert _parse(tag) >= MINIMUM_VERSION, (
                f"{COMPOSE_FILE.name} pins LibreChat {tag}; Agent Skills and subagents "
                f"ship in v{'.'.join(map(str, MINIMUM_VERSION))} and later. Three p0 "
                "features are blocked on this number."
            )

    def test_the_bundle_and_the_cluster_pin_the_same_version(self):
        """They have drifted before (rd 634), and drift makes every local proof void.

        Worse than void, in one direction: the two deployments read the SAME
        bundle/librechat/librechat.yaml, and a config carrying a v0.8.6+ key (this one
        carries `mcpSettings`) is rejected outright by v0.8.0's strict schema parse —
        whereupon v0.8.0 drops the entire custom configuration and comes up healthy with
        no gateway endpoint at all.
        """
        compose_tags = set(_pinned_versions(COMPOSE_FILE))
        cluster_tags = set(_pinned_versions(K8S_CHAT_FILE))
        assert compose_tags, f"no LibreChat image pin found in {COMPOSE_FILE}"
        assert cluster_tags, f"no LibreChat image pin found in {K8S_CHAT_FILE}"
        assert compose_tags == cluster_tags, (
            f"the compose bundle pins {sorted(compose_tags)} and the cluster pins "
            f"{sorted(cluster_tags)} — one of them is running a surface nothing tests"
        )

    def test_the_running_container_is_the_pinned_version(self, chat_container):
        """The pin is a claim about a file; this is the image that is actually serving."""
        running = subprocess.run(
            ["docker", "inspect", "-f", "{{.Config.Image}}", chat_container],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert running in set(
            f"ghcr.io/danny-avila/librechat:{t}" for t in _pinned_versions(COMPOSE_FILE)
        ), f"the running chat container is {running}, which is not what compose pins"

    def test_config_schema_version_matches_the_running_image(
        self, chat_container, librechat_config
    ):
        """Done-condition 5, asked of the image rather than of a changelog.

        The image is the authority: `Constants.CONFIG_VERSION` in
        packages/data-provider. Read out of the container so this keeps testing the claim
        after the next bump instead of pinning a string that goes stale silently.

        A mismatch is only a log line — packages/api/src/app/checks.ts#checkConfig prints
        "Outdated Config version" and carries on — which is exactly why it needs a test:
        nothing else in the stack ever notices.
        """
        expected = subprocess.run(
            ["docker", "exec", chat_container, "node", "-e",
             "const{Constants}=require('librechat-data-provider');"
             "process.stdout.write(Constants.CONFIG_VERSION)"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert expected, "could not read CONFIG_VERSION out of the chat container"
        assert librechat_config["version"] == expected, (
            f"librechat.yaml declares config version {librechat_config['version']} but "
            f"the running image expects {expected}"
        )


# ---------------------------------------------------------------------------
# Done-condition 4: what librechat.yaml configures must still WORK
# ---------------------------------------------------------------------------

class TestTheConfigIsStillHonoured:
    def test_the_surface_loaded_our_config_at_all(self, startup_config):
        """The silent-null failure: a rejected config leaves a healthy, empty surface.

        Asserted through what the client is served, not through the file on disk. If
        loadCustomConfig had returned null, /api/config would carry no modelSpecs and the
        gateway endpoint would be absent — while /health still answered 200.
        """
        specs = startup_config.get("modelSpecs") or {}
        assert specs.get("list"), (
            "the surface served no modelSpecs — librechat.yaml was not loaded, so the "
            "surface is running on defaults with no route to the gateway"
        )

    def test_the_gateway_endpoint_is_the_one_offered(self, chat_session, chat_url):
        """ENDPOINTS=custom,agents, checked at the surface (finding 4).

        tests/test_scope_items.py already asserts the direct-provider bypass is closed.
        This asserts the other half — that closing it did not also take away the endpoint
        we actually route through, which is what happened on the cluster when the list
        was narrowed to "custom" alone.
        """
        endpoints = chat_session.get(
            f"{chat_url}/api/endpoints", headers=_auth(chat_session, chat_url),
            timeout=TIMEOUT,
        ).json()
        assert "Enterprise AI" in endpoints, (
            f"the gateway endpoint is not offered; surface offers {sorted(endpoints)}"
        )
        assert endpoints["Enterprise AI"].get("type") == "custom"
        assert endpoints["Enterprise AI"].get("userProvide") is False

    def test_the_agents_endpoint_offers_skills_and_code_execution(
        self, chat_session, chat_url
    ):
        """The whole point of the upgrade, asserted as a capability the surface reports.

        Fails on v0.8.0, which has neither `skills` nor `subagents` in its capability
        enum. Also answers the item's "does the agents endpoint need skills enabled
        explicitly?" — it does not: `skills` is in librechat-data-provider's
        `defaultAgentCapabilities`, so it is on unless a config removes it. The assertion
        is on the served list rather than on that source fact, so if a later release makes
        it opt-in, this fails instead of quietly regressing 6ff and 082.
        """
        endpoints = chat_session.get(
            f"{chat_url}/api/endpoints", headers=_auth(chat_session, chat_url),
            timeout=TIMEOUT,
        ).json()
        assert "agents" in endpoints, (
            "the agents endpoint is absent — MCP servers, memory, tools, file search and "
            "web search all live there (ENDPOINTS must be 'custom,agents', not 'custom')"
        )
        capabilities = set(endpoints["agents"].get("capabilities") or [])
        for required in ("skills", "execute_code", "artifacts", "subagents"):
            assert required in capabilities, (
                f"the agents endpoint does not offer {required!r}; it offers "
                f"{sorted(capabilities)}"
            )

    def test_our_mcp_server_is_connected_and_its_tool_is_offered(
        self, chat_session, chat_url
    ):
        """mcp-echo, still callable — proven by the tool list the surface fetched from it.

        This is the assertion that fails without `mcpSettings.allowedAddresses` in
        librechat.yaml: v0.8.6+ blocks MCP hosts in private IP space as SSRF targets, and
        the surface then serves `{"servers": {}}` here while logging one line at boot and
        nothing afterwards.

        The description asserted below is fetched live from OUR server over
        streamable-http (mcp-echo/), not read from librechat.yaml, so this cannot pass
        against a config that merely mentions the server.
        """
        listed = chat_session.get(
            f"{chat_url}/api/mcp/tools", headers=_auth(chat_session, chat_url),
            timeout=TIMEOUT,
        )
        assert listed.status_code == 200, listed.text
        servers = listed.json().get("servers") or {}
        assert "echo" in servers, (
            f"the surface offers no tools from the 'echo' MCP server: {servers} — "
            "it is configured under mcpServers but the surface could not reach or was "
            "not permitted to reach it"
        )
        tools = {t["name"]: t for t in servers["echo"].get("tools") or []}
        assert "echo" in tools, f"the echo server exposed no 'echo' tool: {tools}"
        assert tools["echo"]["pluginKey"] == "echo_mcp_echo"
        assert "ECHO:" in (tools["echo"].get("description") or ""), (
            "the tool description did not come from our server — the surface is showing "
            "a placeholder rather than what mcp-echo actually returned"
        )

    def test_every_model_spec_still_renders_artifacts(self, startup_config):
        """Artifacts, asserted where they are actually switched on.

        NOT via `interface.artifacts`. That key is in bundle/librechat/librechat.yaml and
        has never done anything: it is absent from librechat-data-provider's
        `interfaceSchema` in BOTH v0.8.0 and v0.8.7, so zod strips it, and
        packages/data-schemas/src/app/interface.ts never reads it. What turns artifacts on
        is `preset.artifacts` on each model spec, which is what this asserts. Checked
        against the served config, so it stays true only while the surface agrees.
        """
        specs = (startup_config.get("modelSpecs") or {}).get("list") or []
        assert specs, "no model specs served"
        for spec in specs:
            assert spec.get("preset", {}).get("artifacts") == "default", (
                f"model spec {spec.get('name')!r} does not enable artifacts: "
                f"{spec.get('preset')}"
            )

    def test_the_interface_artifacts_key_is_inert(self, startup_config, librechat_config):
        """Locks in the finding above so nobody 'fixes' artifacts by trusting that key.

        If a future LibreChat starts honouring `interface.artifacts`, this fails and the
        comment in librechat.yaml gets corrected — which is the outcome we want, rather
        than the config quietly meaning something new.
        """
        assert librechat_config["interface"].get("artifacts") is True, (
            "this test exists to document that the key is inert; if it was removed from "
            "librechat.yaml, delete this test too"
        )
        assert "artifacts" not in (startup_config.get("interface") or {}), (
            "the surface now serves interface.artifacts — the config schema has started "
            "honouring a key this project has always had set but never relied on; "
            "reconcile librechat.yaml's comment with the new behaviour"
        )

    def test_the_model_specs_are_the_ones_we_configured(
        self, startup_config, librechat_config
    ):
        """The specs the browser opens with, compared against the file, name for name.

        The `default: true` flag is the difference between a surface that opens ready to
        type and one that opens with nothing selected.
        """
        served = {s["name"]: s for s in (startup_config["modelSpecs"]["list"])}
        configured = {s["name"]: s for s in librechat_config["modelSpecs"]["list"]}
        assert set(served) == set(configured), (
            f"served specs {sorted(served)} != configured {sorted(configured)}"
        )
        for name, spec in configured.items():
            assert served[name]["preset"]["model"] == spec["preset"]["model"]
            assert served[name]["preset"]["endpoint"] == spec["preset"]["endpoint"]
            assert bool(served[name].get("default")) == bool(spec.get("default")), (
                f"spec {name!r} default flag was not preserved"
            )
        defaults = [n for n, s in served.items() if s.get("default")]
        assert len(defaults) == 1, (
            f"exactly one spec must open by default; got {defaults}"
        )

    def test_the_model_picker_and_parameters_panel_are_actually_switched_on(
        self, startup_config
    ):
        """enterpriseaiframework-282, the config half of the claim.

        The OPPOSITE trap from `interface.artifacts` above: `modelSelect` and
        `parameters` ARE real, honoured keys of data-provider's `interfaceSchema` — but
        that schema's `.default({modelSelect:true, parameters:true, ...})` fires only
        when the whole `interface:` key is absent from librechat.yaml, and this file has
        always set one (for privacyPolicy/termsOfService/artifacts/memories). Every key
        under `interface:` that this file does not also list therefore comes back
        `undefined` from zod, which `/api/config` serves as `false` — not as "defaulted
        to true". Measured on this exact file before `modelSelect`/`parameters` were
        added to it: both `false` in a live `/api/config` response, with the gateway
        catalogue and modelSpecs both otherwise fully configured and reachable by a raw
        API call (see tests/test_scope_items.py::TestModelPickerAndReasoningEffort,
        which drives turns directly and is unaffected by this key either way).
        `modelSelect: false` hides the model dropdown — nobody can reach the fetched
        catalogue no matter how large it is. `parameters: false` hides the Parameters
        panel, the only place `reasoning_effort` is exposed to a user. Checked against
        the served config, not the file, so this stays true only while the surface
        agrees.
        """
        interface = startup_config.get("interface") or {}
        assert interface.get("modelSelect") is True, (
            f"interface.modelSelect is {interface.get('modelSelect')!r}, not True — the "
            "model picker is not rendered regardless of what the gateway catalogue offers"
        )
        assert interface.get("parameters") is True, (
            f"interface.parameters is {interface.get('parameters')!r}, not True — the "
            "Parameters panel (reasoning_effort's only home) is not rendered"
        )

    def test_automatic_memory_extraction_is_explicitly_enabled(self, librechat_config):
        """Fails on the config this item started from.

        From v0.8.6, packages/data-schemas/src/app/memory.ts#isMemoryAgentEnabled returns
        false unless `memory.agent.enabled === true`. v0.8.0 extracted unconditionally, so
        the identical file means "extract" on one version and "never extract" on the
        other, with one startup warning as the only difference.
        """
        agent = librechat_config["memory"]["agent"]
        assert agent.get("enabled") is True, (
            "memory.agent.enabled is not true — on v0.8.6+ automatic memory extraction "
            "is opt-in, so this surface stores nothing a user asks it to remember"
        )
        assert agent["provider"] == "Enterprise AI", (
            "the memory extraction agent must route through our own gateway endpoint, or "
            "it is a model call outside the ledger and the audit trail"
        )

    def test_a_memory_outlives_the_session_that_wrote_it(self, chat_session, chat_url):
        """Memory across a session boundary, done-condition 4.

        Written through the surface's own API on one authenticated session, then read back
        on a SECOND, independently established OIDC login — a different token, a different
        cookie jar. That is the boundary that matters for the claim "a preference stated
        once is available later": it proves the value lives in Mongo behind
        /api/memories, not in anything the first session was holding.

        What this deliberately does NOT claim: that the model chose to call `set_memory`.
        That is a property of the model, needs an upstream that tool-calls, and is proven
        against the cluster in tests-live/test_memory.py. The bundle's fakeprovider has no
        tool-calling behaviour at all, so asserting it here would need a stub standing in
        for the very thing under test.
        """
        # LibreChat's MemoryEntry schema validates keys as lowercase letters and
        # underscores only — no digits, no hyphens. A hex suffix is rejected with a 500.
        suffix = "".join(chr(ord("a") + b % 26) for b in uuid.uuid4().bytes[:8])
        key = f"probe_{suffix}"
        value = f"remembered-{uuid.uuid4().hex[:12]}"
        created = chat_session.post(
            f"{chat_url}/api/memories",
            headers=_auth(chat_session, chat_url),
            json={"key": key, "value": value},
            timeout=TIMEOUT,
        )
        assert created.status_code in (200, 201), (
            f"the surface would not store a memory ({created.status_code}): "
            f"{created.text[:300]}"
        )

        second = oidc_login.login(chat_url, DOGFOOD_USER, DOGFOOD_PASSWORD)
        try:
            read_back = second.get(
                f"{chat_url}/api/memories", headers=_auth(second, chat_url),
                timeout=TIMEOUT,
            )
            assert read_back.status_code == 200, read_back.text
            stored = {m["key"]: m.get("value") for m in read_back.json()["memories"]}
            assert key in stored, (
                f"a memory written in one session was not visible in a new one: "
                f"{sorted(stored)}"
            )
            assert stored[key] == value, (
                f"the memory came back changed: {stored[key]!r} != {value!r}"
            )
        finally:
            chat_session.delete(
                f"{chat_url}/api/memories/{key}",
                headers=_auth(chat_session, chat_url), timeout=TIMEOUT,
            )
            second.close()


# ---------------------------------------------------------------------------
# A real turn, on the new protocol
# ---------------------------------------------------------------------------

class TestARealTurn:
    """The custom endpoint through our gateway — driven, not inspected.

    Everything above reads what the surface says about itself. This sends a message the
    way the browser does and asserts on the answer that came back through
    surface -> gateway -> upstream. The reply is a sha256 of model+prompt computed by
    fakeprovider, and the prompt carries a fresh nonce, so it cannot be satisfied by a
    cached reply, a fixture, or a surface that answered without reaching the gateway.
    """

    def test_a_message_goes_out_through_the_gateway_and_the_answer_comes_back(
        self, chat_session, chat_url
    ):
        import hashlib

        nonce = uuid.uuid4().hex[:12]
        prompt = f"say hello {nonce}"
        reply = chat_turn.send_turn(
            chat_session, chat_url, prompt, model="fake-large",
            endpoint="Enterprise AI", mcp_servers=["echo"],
            headers=_auth(chat_session, chat_url),
        )
        text = chat_turn.reply_text(reply)

        # fakeprovider/app.py: reply_text() = sha256(f"{model}\x00{prompt}")[:12].
        # The upstream model name is the one the gateway maps "fake-large" onto.
        digest = hashlib.sha256(f"fake-gpt-large\x00{prompt}".encode()).hexdigest()[:12]
        assert text.strip() == f"[fake-provider fake-gpt-large] ack {digest}", (
            f"the reply is not the one the upstream computes for this exact prompt: "
            f"{text!r}"
        )
        assert reply.get("model") == "fake-large", (
            f"the turn was answered by {reply.get('model')!r}, not the requested model"
        )

    def test_the_memory_extraction_agent_actually_runs_on_a_turn(
        self, chat_session, chat_url, chat_container
    ):
        """`memory.agent.enabled: true` observed doing something, not just being present.

        The config assertion above reads the file. This sends a turn and asserts the
        surface actually dispatched the memory-extraction agent for it — the behaviour
        that v0.8.6 turned off by default and that the file-level assertion alone cannot
        distinguish from "the key is set and ignored".

        In the bundle that agent then FAILS, deterministically: it is configured to run on
        glm-5.2@deepinfra (a real provider model, so extraction is reliable in production)
        and the bundle's catalogue is fake-only. `[MemoryAgent] Failed to process memory`
        is therefore the expected line here, and it is still the fact under test — the
        agent was invoked. With `enabled` removed the line does not appear at all, because
        nothing is invoked. Verified both ways against this container.
        """
        started = time.time()
        chat_turn.send_turn(
            chat_session, chat_url, f"remember that my favourite colour is "
            f"{uuid.uuid4().hex[:8]}",
            model="fake-large", endpoint="Enterprise AI",
            headers=_auth(chat_session, chat_url),
        )
        assert "[MemoryAgent]" in _wait_for_log(
            chat_container, started, "[MemoryAgent]"
        ), (
            "the surface never invoked the memory-extraction agent for a turn — "
            "automatic memory extraction is off, so nothing a user asks to be remembered "
            "will be"
        )

    def test_the_title_model_is_the_one_librechat_yaml_names(
        self, chat_session, chat_url, chat_container, librechat_config
    ):
        """titleModel, asserted from what the surface actually did with it.

        Conversation titling is a separate model call the surface makes on its own; there
        is no API that reports which model it used. So: send a turn, then read the chat
        container's log for the titling call and assert it named exactly the model
        configured as `titleModel`.

        In the bundle that call FAILS, deterministically and by design — `titleModel` is
        a real provider model (glm-4.7@deepinfra) and the bundle's catalogue is fake-only,
        so the gateway answers 400 "Invalid model name passed in model=<name>". That 400
        is the evidence: it carries the model name the surface chose, which is the fact
        under test. It is also why this reads a log rather than an assertion endpoint —
        and why the same claim against a real catalogue belongs in tests-live.

        The defect this guards is real and was shipped: titleModel was once "fake-small",
        and every conversation on the cluster was titled "[fake-provider fake-gpt-small]
        ack <hex>". A titling model that silently reverts to a stub looks like a broken
        history, not a broken config.
        """
        configured = librechat_config["endpoints"]["custom"][0]["titleModel"]
        assert librechat_config["endpoints"]["custom"][0]["titleConvo"] is True

        started = time.time()
        chat_turn.send_turn(
            chat_session, chat_url, f"title me {uuid.uuid4().hex[:8]}",
            model="fake-large", endpoint="Enterprise AI",
            headers=_auth(chat_session, chat_url),
        )

        combined = _wait_for_log(chat_container, started, "titleConvo")
        assert "titleConvo" in combined, (
            "the surface never attempted to title the conversation — titleConvo is "
            "configured true but nothing tried to run it"
        )
        assert f"model={configured}" in combined, (
            f"the titling call did not use the configured titleModel {configured!r}; "
            "if it fell back to a stub, every conversation gets a stub's name"
        )


# ---------------------------------------------------------------------------
# The chat_turn helper's other branch
# ---------------------------------------------------------------------------

class TestTheTurnHelperHandlesBothProtocols:
    """The v0.8.0 branch of tests/chat_turn.py, which the bundle can no longer exercise.

    The bundle now runs v0.8.7, so every test above goes down the JSON-handle path. The
    cluster still runs v0.8.0 and tests-live/ still drives it, so the SSE path has to keep
    working — and an untested branch in a shared helper is how a live suite starts failing
    for a reason that has nothing to do with what it tests.

    A stub client, not a stub surface: the frames below are the real shape LibreChat
    v0.8.0 emits (`data:` lines, a terminal frame with "final": true), and the only thing
    being faked is the transport. Nothing about the v0.8.7 path is stubbed anywhere —
    that path is exercised against the running container.
    """

    class _Response:
        def __init__(self, status_code, headers, text):
            self.status_code = status_code
            self.headers = headers
            self.text = text

        def json(self):
            import json as _json
            return _json.loads(self.text)

    class _Client:
        def __init__(self, response):
            self._response = response

        def post(self, *_args, **_kwargs):
            return self._response

    def test_the_legacy_sse_post_still_yields_a_conversation_id(self):
        body = (
            'data: {"created":true}\n\n'
            'data: {"final":true,"conversation":{"conversationId":"abc-123"},'
            '"responseMessage":{"text":"hi"}}\n\n'
        )
        client = self._Client(self._Response(
            200, {"content-type": "text/event-stream"}, body,
        ))
        conversation_id, final = chat_turn.start_turn(
            client, "http://chat.invalid",
            chat_turn.build_payload("hi", "m", "Enterprise AI"),
        )
        assert conversation_id == "abc-123"
        assert final["final"] is True

    def test_an_error_frame_is_reported_as_an_error_not_as_silence(self):
        body = 'data: {"text":"upstream exploded"}\n\n'
        client = self._Client(self._Response(
            200, {"content-type": "text/event-stream"}, body,
        ))
        with pytest.raises(AssertionError, match="upstream exploded"):
            chat_turn.start_turn(
                client, "http://chat.invalid",
                chat_turn.build_payload("hi", "m", "Enterprise AI"),
            )

    def test_the_json_handle_shape_is_recognised_as_a_job_not_as_an_empty_stream(self):
        """The regression that motivated the helper: parsed as SSE this yields nothing."""
        client = self._Client(self._Response(
            200, {"content-type": "application/json; charset=utf-8"},
            '{"streamId":"s-1","conversationId":"c-1","status":"started"}',
        ))
        conversation_id, final = chat_turn.start_turn(
            client, "http://chat.invalid",
            chat_turn.build_payload("hi", "m", "Enterprise AI"),
        )
        assert conversation_id == "c-1"
        assert final is None
        assert chat_turn._sse_events(
            '{"streamId":"s-1","conversationId":"c-1","status":"started"}'
        ) == [], "a JSON handle must not be mistakable for a stream carrying frames"

    def test_reply_text_reads_both_persisted_message_shapes(self):
        """v0.8.6+ persists content blocks and leaves `text` empty; v0.8.0 used `text`."""
        assert chat_turn.reply_text(
            {"text": "", "content": [{"type": "text", "text": "block form"}]}
        ) == "block form"
        assert chat_turn.reply_text({"text": "flat form", "content": []}) == "flat form"
        assert chat_turn.reply_text(
            {"text": "", "content": [{"type": "tool_call", "tool_call": {}}]}
        ) == ""
