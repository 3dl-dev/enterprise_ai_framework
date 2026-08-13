"""Attaching a Hermes agent console: whose agent you reach, and that exec is the door.

WHY THIS FILE IS A SECURITY TEST FIRST AND A FEATURE TEST SECOND

The retarget (docs/design/records/agents-surface-hermes-retarget.md, R3). `/agents/<name>/`
opens a terminal that execs `hermes --tui` inside a resident agent holding a spendable key.
The control plane holds `pods/exec` on every agent pod in the namespace — RBAC cannot narrow
that to "only the caller's own pod" — so the ONLY thing between one user and another user's
live session is `agents.console_target`: it derives `agent-<user>-<name>` from the
authenticated identity and re-checks the object's owner label before naming a pod to exec.
Every rejection case below is somebody trying to cross that, and it is the same guard, not a
second copy, that `test_portal_agents.py` exercises for stop/start/delete.

The console ATTACHES: `hermes --tui` shares the resident daemon's on-disk session and starts
no daemon. That negative — no port is proxied, no process is spawned — is what makes this the
Agents surface and not opencode's web IDE (the conflation the retarget removed).

WHAT IS REAL HERE AND WHAT IS NOT

Real, exercised as shipped: `app.agent_console` (the page, the redirect, the exec URL, the
owner-scoped socket rejection) and `app.agents.console_target` (name derivation, the
owner-label guard, the running-pod requirement) reached over the same fake API server
`test_portal_agents.py` builds. The one claim a fake apiserver cannot support — that the
browser⇄`pods/exec`⇄`hermes --tui` byte bridge actually carries a terminal — is proven
against live k3s in tests-live/test_agent_console.py, where the TUI is driven in the real pod.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

# The fake apiserver, ledger stubs and SA files are the same objects test_portal_agents.py
# builds — imported, not copied, so the two files cannot drift on what Kubernetes is. The
# `cluster` fixture is imported so pytest resolves it here by name.
from test_portal_agents import (  # noqa: E402
    FakeCluster,  # noqa: F401 - imported for parity / future use
    _SA_DIR,  # noqa: F401
    _stub_ledger,  # noqa: F401
    cluster,  # noqa: F401 - the fixture
)

from app import agent_console  # noqa: E402


def console_client(user: str) -> TestClient:
    """The console router, reached the way the oauth2-proxy sidecar reaches it — loopback
    peer and the sidecar's identity header, so the shipped `require_user` derives the name
    and no test hands an endpoint an identity it did not authenticate."""
    api = FastAPI()
    api.include_router(agent_console.router)
    c = TestClient(api, client=("127.0.0.1", 41000), raise_server_exceptions=False)
    c.headers.update({"X-Auth-Request-Preferred-Username": user})
    return c


# ---------------------------------------------------------------- the exec contract


def test_exec_runs_hermes_tui_with_a_tty_in_the_named_pod():
    url = agent_console._exec_url(
        "enterprise-ai", "agent-alice-helper-abc", "agent", ["hermes", "--tui"])
    assert url.startswith("wss://"), "exec is a TLS websocket to the API server"
    assert "/api/v1/namespaces/enterprise-ai/pods/agent-alice-helper-abc/exec" in url
    assert "container=agent" in url
    # A real terminal needs a tty and stdin, and the command is FIXED — the caller supplies
    # no part of it, so exec can never be turned into an arbitrary shell.
    assert "tty=true" in url and "stdin=true" in url and "stdout=true" in url
    assert "command=hermes" in url and "command=--tui" in url


# ---------------------------------------------------------------- the owner attaches


def test_the_owner_gets_a_terminal_page_under_its_own_prefix(cluster):
    cluster.add_agent("alice", "helper")
    r = console_client("alice").get("/agents/helper/")
    assert r.status_code == 200, r.text
    body = r.text
    # A self-hosted xterm (no CDN), opening the exec bridge under this agent's own prefix.
    # The socket path is built from the instance name at runtime, so assert the slug is
    # embedded and the page opens the `/…/ws` bridge.
    assert "/portal/static/xterm.min.js" in body
    assert 'var NAME = "helper"' in body
    assert '"/agents/" + NAME + "/ws"' in body
    # It is a terminal, not opencode's web IDE — the conflation this retarget removed.
    assert "opencode" not in body.lower()


def test_the_bare_path_redirects_to_the_trailing_slash(cluster):
    cluster.add_agent("alice", "helper")
    r = console_client("alice").get("/agents/helper", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/agents/helper/"


# ---------------------------------------------------------------- reject


def test_a_second_user_cannot_open_another_users_console(cluster):
    cluster.add_agent("alice", "helper")
    # 404, not 403: a distinct 403 would confirm to a prober that an agent by this name
    # exists and belongs to somebody — the one fact console_target keeps private.
    assert console_client("mallory").get("/agents/helper/").status_code == 404


def test_a_hyphen_collision_cannot_open_another_users_console(cluster):
    # user "alice-bot" + agent "two"  and  user "alice" + agent "bot-two"  derive ONE object
    # name (agent-alice-bot-two). Deriving from identity is not enough; the owner-label check
    # is what refuses alice the console of alice-bot's agent.
    cluster.add_agent("alice-bot", "two")
    assert console_client("alice").get("/agents/bot-two/").status_code == 404


def test_a_stopped_agent_is_a_409_not_a_blank_terminal(cluster):
    # replicas 0, no running pod: exec has nothing to attach to. A clear 409 ("start it")
    # beats a terminal that opens and then fails silently on connect.
    cluster.add_agent("alice", "helper", replicas=0, running=False)
    assert console_client("alice").get("/agents/helper/").status_code == 409


def test_a_non_owner_websocket_is_closed_before_it_is_bridged(cluster):
    cluster.add_agent("alice", "helper")
    client = console_client("mallory")
    # console_target raises 404 for a non-owner, so the socket is closed (1008) before it is
    # ever accepted or connected to a pod — the reject happens in front of exec, not after.
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/agents/helper/ws"):
            pass
