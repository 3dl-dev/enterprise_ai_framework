"""The chat surface must not come up serving a dead login — enterpriseaiframework-6c9.

THE OUTAGE THIS PINS. LibreChat registers its OpenID passport strategy exactly once, at
boot, by fetching the issuer's discovery document. If that fetch fails it logs "OpenID
Connect configuration failed - strategy not registered." and NEVER retries. On 2026-08-05
the cluster rebooted and `chat` started before Keycloak+Caddy were serving the public
issuer; the strategy never registered, and for four days every visit 500'd on
`/oauth/openid` ("Unknown authentication strategy openid") — with `OPENID_AUTO_REDIRECT`
the bare landing page bounced straight into that 500. The whole time `/health` returned
200 and every pod was Running/Ready: this failure is invisible to ordinary k8s health.

WHY THE BUNDLE NEVER HIT IT, AND WHY THE CLUSTER DID. The compose bundle orders its start
with `depends_on: identity: condition: service_healthy`, so LibreChat there always boots
after Keycloak is healthy. k8s has no such ordering. The fix is an initContainer that IS
that ordering: it blocks until the same public discovery URL the app is about to fetch
returns 200. A startupProbe on `/oauth/openid` is the second line — a boot that comes up
without a registered strategy (302 vs 500) fails startup and is restarted instead of
serving a broken login.

These are mostly static checks — they cannot prove the initContainer runs in-cluster (that
was demonstrated live when the fix went in), but they prove the guards have not been
deleted, which is the realistic regression. The one demonstration below runs the
initContainer's ACTUAL wait-loop against a server that fails then recovers, so the retry
logic is exercised, not merely asserted (Testing Supremacy: demonstrate, don't assert).
"""

from __future__ import annotations

import http.server
import socket
import subprocess
import threading
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
K8S_CHAT = REPO / "deploy" / "k8s" / "50-chat.yaml"
COMPOSE = REPO / "bundle" / "docker-compose.yml"
SMOKE = REPO / "deploy" / "bin" / "smoke.sh"


def _chat_deployment() -> dict:
    docs = list(yaml.safe_load_all(K8S_CHAT.read_text()))
    deploys = [d for d in docs if d and d.get("kind") == "Deployment"]
    assert len(deploys) == 1, "expected exactly one Deployment in 50-chat.yaml"
    return deploys[0]


def _pod_spec() -> dict:
    return _chat_deployment()["spec"]["template"]["spec"]


def _init_container(name: str) -> dict:
    inits = {c["name"]: c for c in _pod_spec().get("initContainers", [])}
    assert name in inits, f"initContainer {name!r} is missing from 50-chat.yaml"
    return inits[name]


def _librechat_container() -> dict:
    containers = {c["name"]: c for c in _pod_spec()["containers"]}
    assert "librechat" in containers, "librechat container missing"
    return containers["librechat"]


# ---------------------------------------------------------------- static guards

def test_chat_has_a_wait_for_oidc_initcontainer_that_gates_on_the_issuer():
    init = _init_container("wait-for-oidc")
    script = "\n".join(init.get("args", []))
    assert ".well-known/openid-configuration" in script, (
        "wait-for-oidc must poll the issuer's discovery document — the exact fetch LibreChat "
        "makes once at boot and never retries"
    )
    assert "OPENID_ISSUER" in script, "wait-for-oidc must build its URL from $OPENID_ISSUER"

    env = {e["name"]: e for e in init.get("env", [])}
    assert "OPENID_ISSUER" in env, "wait-for-oidc needs OPENID_ISSUER in its env"
    ref = env["OPENID_ISSUER"].get("valueFrom", {}).get("secretKeyRef", {})
    assert ref.get("key") == "OPENID_ISSUER", (
        "OPENID_ISSUER must come from the same secret key the app uses, so the initContainer "
        "waits on the exact URL the app will fetch"
    )


def test_wait_for_oidc_does_not_reuse_the_librechat_image():
    """A version-pinned librechat image here would read as a compose/cluster mismatch to
    tests/test_chat_surface_version.py, which compares every `ghcr.io/danny-avila/librechat:`
    pin across the two files. The cluster issuer has a public cert, so a plain curl image is
    both sufficient and correct."""
    image = _init_container("wait-for-oidc")["image"]
    assert "danny-avila/librechat" not in image, (
        "wait-for-oidc must not use the librechat image — it would break the image-pin parity "
        "check in test_chat_surface_version.py"
    )


def test_chat_has_a_startup_probe_that_detects_an_unregistered_strategy():
    probe = _librechat_container().get("startupProbe")
    assert probe, "librechat needs a startupProbe that catches an unregistered OpenID strategy"
    http_get = probe.get("httpGet", {})
    assert http_get.get("path") == "/oauth/openid", (
        "the startupProbe must hit /oauth/openid: it returns 302 once the strategy is "
        "registered and 500 while it is not, so a bad boot fails startup and is restarted "
        "instead of serving a dead login behind a green /health"
    )
    assert http_get.get("port") == 3080


def test_the_bundle_keeps_its_equivalent_ordering_guard():
    """The initContainer is the k8s equivalent of the bundle's start ordering. If someone
    removes the compose guard, this reminds them the two surfaces protect the same thing by
    different mechanisms."""
    compose = yaml.safe_load(COMPOSE.read_text())
    chat = compose["services"]["chat"]
    identity_dep = chat.get("depends_on", {}).get("identity", {})
    assert identity_dep.get("condition") == "service_healthy", (
        "the bundle's chat must still wait on identity being healthy — its equivalent of the "
        "cluster's wait-for-oidc initContainer"
    )


def test_smoke_proves_the_login_strategy_is_registered():
    body = SMOKE.read_text()
    assert "/oauth/openid" in body, (
        "smoke.sh must send the request that actually broke — a deploy where /health is 200 "
        "but /oauth/openid 500s is exactly the outage, and 'pods Running' does not catch it"
    )
    assert '"302"' in body or "302" in body, (
        "smoke.sh must assert /oauth/openid returns 302 (strategy registered), not merely 200"
    )


# ---------------------------------------------------------------- demonstration

class _FlakyThenReady(http.server.BaseHTTPRequestHandler):
    """500 for the first two requests, 200 after — the boot race, compressed. The class
    attribute counts across the whole server so the initContainer's retry loop has something
    to actually retry against."""

    hits = 0

    def do_GET(self):  # noqa: N802
        type(self).hits += 1
        code = 200 if type(self).hits >= 3 else 500
        self.send_response(code)
        self.end_headers()
        self.wfile.write(b"{}" if code == 200 else b"not ready")

    def log_message(self, *_args):  # keep test output quiet
        pass


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_the_actual_wait_loop_retries_until_the_issuer_is_ready():
    """Run the initContainer's REAL script (`sh -c <args>`, exactly as k8s runs it) against a
    server that 500s twice then 200s. It must keep going past the failures and exit 0 only
    once the issuer is ready — the behaviour that was missing when LibreChat gave up after a
    single failed fetch."""
    init = _init_container("wait-for-oidc")
    assert init.get("command") == ["sh", "-c"], (
        "this demonstration runs the script as `sh -c`; if the command changed, update it here"
    )
    script = init["args"][0]

    _FlakyThenReady.hits = 0
    port = _free_port()
    server = http.server.HTTPServer(("127.0.0.1", port), _FlakyThenReady)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        result = subprocess.run(
            ["sh", "-c", script],
            env={"OPENID_ISSUER": f"http://127.0.0.1:{port}/realms/enterprise-ai"},
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        server.shutdown()

    assert result.returncode == 0, (
        f"the wait-loop should exit 0 once the issuer returns 200; got {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert _FlakyThenReady.hits >= 3, (
        f"the loop must retry past the failures — expected >=3 requests, saw "
        f"{_FlakyThenReady.hits}. A loop that gave up early (the original bug) would stop at 1."
    )
