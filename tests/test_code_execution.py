"""enterpriseaiframework-082: chat runs code in a real sandbox and shows real output.

DONE condition, verbatim from the item: ask the chat to compute something the model
CANNOT produce by prediction and get the correct answer back; the effect must be
observable AFTERWARD (a file in the session store, a log entry, a sandbox artifact), not
merely plausible in the reply.

Two independent proofs, deliberately not one:

1. CORRECTNESS THE MODEL CANNOT FAKE. fakeprovider computes nothing (see
   tests/test_fakeprovider_execute_code.py and fakeprovider/app.py) — it can only relay
   what a real sandbox actually ran. A sha256 of a nonce generated fresh by THIS test run
   is not in any training data and not derivable from the prompt alone; the only way it
   can come back correct is that codeapi-sandbox really executed the command.

2. AN ARTIFACT OBSERVABLE AFTER THE FACT, INDEPENDENT OF THE REPLY. Every real /exec call
   registers `session:<id>` in codeapi's own Valkey (service/src/service/router.ts) before
   the job runs — this test counts that key set before and after the turn and asserts it
   grew, straight from the session store rather than from anything the model said.

The no-path-to-the-master-key invariant (Finding 27; "no path to ... the gateway master
key") is checked structurally: codeapi-sandbox's actual container environment is
inspected and asserted to carry none of CODEAPI_*/REDIS_*/AWS_*/S3_*/MINIO_* or anything
matching SECRET|TOKEN|PASSWORD|PRIVATE_KEY — the same set api/src/secure-startup.ts
refuses to boot the sandbox over. If the container is running and answering, that check
already passed once at boot; this asserts it again, directly, rather than trusting that
inference.

RE-DISPATCH (adversary verdict UNRESOLVED, see rd notes): the two classes above proved
that a real sandbox really executes code, but nothing proved WHOSE identity codeapi runs
it under. Flipping LOCAL_MODE to true or CODEAPI_AUTH_PROVIDER to none — where userId
comes from a caller-supplied header instead of the LibreChat-minted JWT — left all of the
above green. Two more classes close that:

3. TestCodeApiRefusesCallerAssertedIdentity (Challenge 1): probes the running codeapi
   container directly and asserts it 401s a caller with no bearer, a caller asserting a
   victim's identity via a `User-Id` header, and a caller with a forged bearer. This is
   only true because bundle/docker-compose.yml sets LOCAL_MODE=false and
   CODEAPI_AUTH_PROVIDER=librechat-jwt for the `codeapi` service — this test is what
   makes that config claim fail loudly if it ever drifts.

4. TestCrossUserSessionIsolation (Challenge 2): logs in as a SECOND real Keycloak user,
   drives a real chat turn for each of two users, and shows their codeapi session-store
   artifacts are namespaced by distinct canonical user ids and that one user's output
   session is unreachable to the other through codeapi's own /download endpoint —
   the "no path to ... another user's workspace" half of Finding 27, exercised live
   rather than inherited from the vendored dependency's own (unrun-by-`make test`)
   upstream tests. See enterpriseaiframework-297.
"""

import hashlib
import json
import re
import subprocess
import time
import uuid

import jwt as pyjwt
import pytest

import chat_turn
import oidc_login
from conftest import compose, idp_admin_token

pytestmark = pytest.mark.usefixtures("stack_up")

TIMEOUT = 180.0
MODEL = "fake-large"
ENDPOINT_NAME = "Enterprise AI"

# chat_session (conftest.py) only carries the session cookie from the OIDC login —
# LibreChat's own API additionally wants a short-lived bearer token minted from that
# session (packages/api's refresh route), the same two-part scheme
# tests/test_scope_items.py's _chat_token/_chat_headers use. Duplicated here rather than
# imported: test_scope_items.py's helper is private to that module by convention, not by
# any accident this file should route around.


def _chat_token(client, chat_url: str) -> str:
    r = client.post(
        f"{chat_url}/api/auth/refresh",
        headers={"Cookie": oidc_login._cookie_header(client)},
        timeout=TIMEOUT,
    )
    assert r.status_code == 200, r.text
    return r.json()["token"]


def _chat_headers(client, chat_url: str) -> dict:
    return {
        "Authorization": f"Bearer {_chat_token(client, chat_url)}",
        "Cookie": oidc_login._cookie_header(client),
    }


def _redis_session_key_count(env) -> int:
    """How many `session:*` keys codeapi's own Valkey currently holds.

    Every real /exec call registers one (service/src/service/router.ts:
    `connection.set('session:'+session_id, sessionKey, 'EX', ...)`) BEFORE the sandbox
    job runs — this is the session store the item's done condition asks for, read
    directly rather than through anything the chat reply claims.
    """
    result = compose(
        "exec", "-T", "codeapi-redis", "valkey-cli", "--no-auth-warning",
        "-a", env["CODEAPI_REDIS_PASSWORD"], "--scan", "--pattern", "session:*",
        check=True,
    )
    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    return len(lines)


def _codeapi_sandbox_env(env) -> list[str]:
    """The ACTUAL environment codeapi-sandbox's container process sees, via `docker
    compose exec ... env` — not the compose file's declared config, which could drift
    from what a real container inherits (e.g. from a base image's own ENV lines)."""
    result = compose("exec", "-T", "codeapi-sandbox", "env", check=True)
    return result.stdout.splitlines()


class TestChatRunsRealCode:
    def test_correct_answer_the_model_cannot_predict_and_an_afterward_artifact(
        self, chat_session, chat_url, env
    ):
        headers = _chat_headers(chat_session, chat_url)
        models = chat_session.get(
            f"{chat_url}/api/models", headers=headers, timeout=TIMEOUT
        ).json().get(ENDPOINT_NAME, [])
        assert MODEL in models, (
            f"{MODEL!r} not offered on {ENDPOINT_NAME!r}: {models} — codeapi wiring "
            "cannot be exercised without it"
        )

        before = _redis_session_key_count(env)

        nonce = uuid.uuid4().hex
        expected_hash = hashlib.sha256(nonce.encode()).hexdigest()
        command = (
            "python3 -c \"import hashlib; "
            f"print(hashlib.sha256(b'{nonce}').hexdigest())\""
        )
        text = f"EXECUTE_BASH:{command}"

        reply = chat_turn.send_turn(
            chat_session, chat_url, text, model=MODEL, endpoint=ENDPOINT_NAME,
            execute_code=True, headers=headers, timeout=TIMEOUT,
        )
        assert reply, "no assistant message was persisted for this turn"
        reply_text = chat_turn.reply_text(reply)

        assert expected_hash in reply_text, (
            f"expected the sandbox-computed sha256 of a nonce THIS TEST RUN generated "
            f"({nonce!r} -> {expected_hash!r}) to appear in the reply verbatim; got: "
            f"{reply_text!r}. fakeprovider cannot compute this itself (see "
            f"fakeprovider/app.py's find_exec_command/find_tool_result — it only ever "
            f"relays a real tool result), so its absence means either the tool call "
            f"never reached codeapi-sandbox, or the sandbox produced a different answer."
        )

        # Not merely plausible in the reply: a fresh session was actually registered in
        # codeapi's own session store while this call was in flight.
        after = _redis_session_key_count(env)
        assert after > before, (
            f"codeapi-redis's session:* key count did not grow ({before} -> {after}) — "
            "the reply contained the right answer but no new /exec session was ever "
            "registered in the session store, which is the artifact the item's done "
            "condition requires be observable AFTERWARD, independent of the reply"
        )

    def test_the_sandbox_container_cannot_see_any_credential(self, env):
        """Finding 27 / the item's own invariant, checked structurally: codeapi-sandbox
        must have no path to another user's workspace, the cluster API, or the gateway
        master key. api/src/secure-startup.ts refuses to BOOT the sandbox if it can see
        anything matching this pattern — a running, answering container already passed
        that check once; this asserts it directly rather than trusting the inference."""
        forbidden = re.compile(
            r"^(CODEAPI_|REDIS_|AWS_|S3_|MINIO_)|SECRET|TOKEN|PASSWORD|PRIVATE_KEY",
            re.IGNORECASE,
        )
        # Two CODEAPI_*-named exceptions, matching api/src/secure-startup.ts's own
        # forbiddenEnvNames() exactly rather than a stricter check this test invents:
        # - SANDBOX_EXECUTION_MANIFEST_PUBLIC_KEY: a PUBLIC key the sandbox needs to
        #   verify the worker's execution manifest. Not a credential — nothing signs or
        #   authenticates with it.
        # - CODEAPI_HARDENED_SANDBOX_MODE: the switch this very check reads to decide
        #   whether to run at all (`if (name === 'CODEAPI_HARDENED_SANDBOX_MODE')
        #   continue`) — it cannot both gate the check and be forbidden by it.
        allowed_exceptions = {
            "SANDBOX_EXECUTION_MANIFEST_PUBLIC_KEY",
            "CODEAPI_HARDENED_SANDBOX_MODE",
        }

        offenders = []
        for line in _codeapi_sandbox_env(env):
            name = line.partition("=")[0]
            if not name or name in allowed_exceptions:
                continue
            if forbidden.search(name):
                offenders.append(name)

        assert not offenders, (
            f"codeapi-sandbox's actual container environment carries variables the "
            f"hardened-sandbox-mode boot check exists to keep out of a pod running "
            f"arbitrary user code: {offenders}"
        )

    def test_pid_1s_live_environ_also_carries_no_credential(self, env):
        """The stronger claim `docker exec ... env` cannot make: `docker exec` starts a
        NEW process inside the container's namespaces and reports ITS OWN environment
        (inherited from the image/compose config), not necessarily what PID 1 is actually
        running with right now. Reading /proc/1/environ directly is what the sandboxed
        code itself would see if it broke out of its own process namespace, so this is
        the live artifact rather than the configured one."""
        forbidden = re.compile(
            r"^(CODEAPI_|REDIS_|AWS_|S3_|MINIO_)|SECRET|TOKEN|PASSWORD|PRIVATE_KEY",
            re.IGNORECASE,
        )
        allowed_exceptions = {
            "SANDBOX_EXECUTION_MANIFEST_PUBLIC_KEY",
            "CODEAPI_HARDENED_SANDBOX_MODE",
        }
        result = compose("exec", "-T", "codeapi-sandbox", "cat", "/proc/1/environ", check=True)
        names = [
            entry.partition("=")[0]
            for entry in result.stdout.split("\x00")
            if entry
        ]
        offenders = [
            name for name in names
            if name not in allowed_exceptions and forbidden.search(name)
        ]
        assert not offenders, (
            f"codeapi-sandbox's PID 1 live environ (not docker exec's own reported env) "
            f"carries variables the hardened-sandbox-mode boot check exists to keep out "
            f"of a pod running arbitrary user code: {offenders}"
        )


# ---------------------------------------------------------------------------
# Re-dispatch, Challenge 1: nothing asserted that codeapi refuses a
# caller-asserted identity. codeapi has no port published to the host
# (bundle/docker-compose.yml), so these probes originate from inside the real
# `chat` container over the bundle's own docker network — the same vantage
# point LibreChat's own outgoing call has, and the same
# `docker exec <chat_container> node -e ...` hop
# test_chat_surface_version.py already uses to inspect the running image from
# the inside.
# ---------------------------------------------------------------------------

_EXEC_IDENTITY_PROBE_SCRIPT = """
(async () => {
  const probes = [
    ["no_auth", {}],
    ["user_id_header", {"User-Id": "victim-0000-0000-0000"}],
    ["forged_bearer", {"Authorization": "Bearer not-a-real.jwt.at-all"}],
  ];
  const out = {};
  for (const [name, headers] of probes) {
    const res = await fetch("http://codeapi:3112/v1/exec", {
      method: "POST",
      headers: Object.assign({"Content-Type": "application/json"}, headers),
      body: JSON.stringify({lang: "python", code: "print(1)"}),
    });
    out[name] = {status: res.status, body: await res.text()};
  }
  process.stdout.write(JSON.stringify(out));
})();
"""


def _node_probe_in_chat(chat_container: str, script: str, timeout: float = 30.0) -> dict:
    """Run a small node script inside the real `chat` container and parse its one line
    of JSON stdout. `chat` has network access to codeapi's internal DNS name; nothing
    outside the bundle's compose network does."""
    result = subprocess.run(
        ["docker", "exec", chat_container, "node", "-e", script],
        capture_output=True, text=True, timeout=timeout,
    )
    assert result.returncode == 0, (
        f"identity probe script failed inside {chat_container} (rc={result.returncode}): "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    return json.loads(result.stdout)


@pytest.fixture(scope="module")
def chat_container(env) -> str:
    """The name of THIS bundle's chat container — see
    test_chat_surface_version.py's identical fixture for why this is derived from the
    compose project name rather than hardcoded."""
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
def _exec_identity_probes(chat_container) -> dict:
    return _node_probe_in_chat(chat_container, _EXEC_IDENTITY_PROBE_SCRIPT)


class TestCodeApiRefusesCallerAssertedIdentity:
    """Challenge 1: "codeapi appears only in test_code_execution.py [...] no test
    asserts that codeapi refuses a caller-asserted identity." bundle/docker-compose.yml
    sets LOCAL_MODE=false and CODEAPI_AUTH_PROVIDER=librechat-jwt for the real `codeapi`
    service, which is what makes all three of these true; if either ever flips, these
    probes flip with them (see the mutation check in the item's dispatch notes)."""

    def test_a_request_with_no_bearer_is_refused(self, _exec_identity_probes):
        outcome = _exec_identity_probes["no_auth"]
        assert outcome["status"] == 401, outcome
        assert json.loads(outcome["body"]) == {"error": "Bearer token is required"}, outcome

    def test_a_caller_supplied_user_id_header_is_not_trusted(self, _exec_identity_probes):
        """A `User-Id` header naming a victim, with no bearer token, must be refused
        exactly like no auth at all — proving the header is never consulted for identity
        outside CODEAPI_AUTH_PROVIDER=none + LOCAL_MODE, neither of which this deployment
        sets (service/src/middleware/auth.ts's `apiKeyAuth`)."""
        outcome = _exec_identity_probes["user_id_header"]
        assert outcome["status"] == 401, (
            f"a caller-supplied User-Id header must not let the caller assert a victim's "
            f"identity; got {outcome}"
        )
        assert json.loads(outcome["body"]) == {"error": "Bearer token is required"}, outcome

    def test_a_forged_bearer_is_rejected(self, _exec_identity_probes):
        outcome = _exec_identity_probes["forged_bearer"]
        assert outcome["status"] == 401, outcome
        assert json.loads(outcome["body"]) == {"error": "Invalid bearer token"}, outcome


# ---------------------------------------------------------------------------
# Re-dispatch, Challenge 2: no test proves the cross-user clause. Log in as a
# second real Keycloak user, drive a real chat turn for each, and show one
# user's codeapi output session is unreachable to the other.
# ---------------------------------------------------------------------------

SECOND_USER = "codeapi-isolation-check"
SECOND_PASSWORD = "codeapi-isolation-check-pw-1"


def _ensure_second_realm_user(env) -> None:
    """Create (or update) a second, real Keycloak user — same admin API
    bundle/bin/ensure-user.sh uses, kept idempotent for the same reason: a rerun of this
    test must not fail because the previous run already created the account."""
    idp = f"http://localhost:{env.get('IDP_PORT', '8082')}"
    realm = env.get("IDP_REALM", "enterprise-ai")
    headers = {"Authorization": f"Bearer {idp_admin_token(env)}"}

    import httpx

    found = httpx.get(
        f"{idp}/admin/realms/{realm}/users",
        headers=headers, params={"username": SECOND_USER, "exact": "true"}, timeout=30,
    ).json()

    profile = {
        "username": SECOND_USER,
        "email": f"{SECOND_USER}@example.invalid",
        "firstName": "Codeapi-Isolation-Check",
        "lastName": "User",
        "enabled": True,
        "emailVerified": True,
        "requiredActions": [],
    }

    if not found:
        r = httpx.post(
            f"{idp}/admin/realms/{realm}/users",
            headers={**headers, "Content-Type": "application/json"}, json=profile, timeout=30,
        )
        assert r.status_code == 201, r.text
        found = httpx.get(
            f"{idp}/admin/realms/{realm}/users",
            headers=headers, params={"username": SECOND_USER, "exact": "true"}, timeout=30,
        ).json()
    else:
        r = httpx.put(
            f"{idp}/admin/realms/{realm}/users/{found[0]['id']}",
            headers={**headers, "Content-Type": "application/json"}, json=profile, timeout=30,
        )
        r.raise_for_status()

    user_id = found[0]["id"]
    r = httpx.put(
        f"{idp}/admin/realms/{realm}/users/{user_id}/reset-password",
        headers={**headers, "Content-Type": "application/json"},
        json={"type": "password", "value": SECOND_PASSWORD, "temporary": False},
        timeout=30,
    )
    r.raise_for_status()


@pytest.fixture(scope="module")
def second_chat_session(chat_url, env):
    """A REAL second OIDC-authenticated chat session — a different Keycloak account,
    logged in through the same authorization-code flow oidc_login.login() drives for the
    bootstrap user, not a synthesized header or claim."""
    _ensure_second_realm_user(env)
    client = oidc_login.login(chat_url, SECOND_USER, SECOND_PASSWORD)
    yield client
    client.close()


def _run_hashing_turn(client, chat_url, headers) -> tuple[str, str]:
    """One real chat turn that runs sandboxed code computing the sha256 of a fresh
    nonce. Returns (nonce, expected_hash) so the caller can assert the answer came back
    correctly, proving this user's own execution genuinely ran."""
    nonce = uuid.uuid4().hex
    expected_hash = hashlib.sha256(nonce.encode()).hexdigest()
    command = (
        "python3 -c \"import hashlib; "
        f"print(hashlib.sha256(b'{nonce}').hexdigest())\""
    )
    text = f"EXECUTE_BASH:{command}"
    reply = chat_turn.send_turn(
        client, chat_url, text, model=MODEL, endpoint=ENDPOINT_NAME,
        execute_code=True, headers=headers, timeout=TIMEOUT,
    )
    assert reply, "no assistant message was persisted for this turn"
    reply_text = chat_turn.reply_text(reply)
    assert expected_hash in reply_text, (
        f"expected the sandbox-computed sha256 of a nonce THIS turn generated "
        f"({nonce!r} -> {expected_hash!r}) in the reply; got {reply_text!r}"
    )
    return nonce, expected_hash


def _new_session_keys(env, before: set) -> dict:
    """The session:* keys that appeared since `before` was captured, mapped to their
    codeapi-redis VALUES (the sessionKey each one resolved to) — not merely a count, so
    the caller can inspect which canonical user each new session belongs to."""
    result = compose(
        "exec", "-T", "codeapi-redis", "valkey-cli", "--no-auth-warning",
        "-a", env["CODEAPI_REDIS_PASSWORD"], "--scan", "--pattern", "session:*",
        check=True,
    )
    after = {ln for ln in result.stdout.splitlines() if ln.strip()}
    new_keys = after - before
    values = {}
    for key in new_keys:
        got = compose(
            "exec", "-T", "codeapi-redis", "valkey-cli", "--no-auth-warning",
            "-a", env["CODEAPI_REDIS_PASSWORD"], "GET", key, check=True,
        )
        values[key] = got.stdout.strip()
    return values


def _mint_codeapi_bearer(env, user_id: str) -> str:
    """Mint a codeapi bearer JWT with the SAME Ed25519 signing key, claim shape, and
    algorithm LibreChat's packages/api/src/auth/codeapi.ts uses to mint one per outgoing
    request (buildClaims/signJwt) — read straight out of bundle/.env, the file the real
    `codeapi`/`chat` containers are themselves configured from.

    `user_id` is never invented by this test: every caller passes in a canonicalUserId
    this test extracted from a REAL, already-persisted codeapi-redis sessionKey that a
    genuine chat turn for a genuinely OIDC-authenticated user produced. This is
    deliberately NOT the same thing Challenge 1 tests — Challenge 1 (above) proves
    codeapi refuses an UNSIGNED, caller-supplied identity (a `User-Id` header, a body
    field); this signs with the actual production private key to reach the orthogonal
    property under test here, which is whether codeapi's session-key ISOLATION logic
    keeps two DIFFERENT genuine principals apart. Intercepting LibreChat's own
    per-request ephemeral mint would prove the identical thing with more moving parts and
    no stronger a guarantee, since `auth_context_hash` is asserted to be a non-empty
    string by codeapi's verifier (service/src/auth/librechat-jwt.ts#validateClaims) and
    never recomputed or compared.
    """
    private_key_pem = env["CODEAPI_JWT_PRIVATE_KEY"].replace("\\n", "\n")
    now = int(time.time())
    claims = {
        "iss": env.get("CODEAPI_JWT_ISSUER", "librechat"),
        "aud": env.get("CODEAPI_JWT_AUDIENCE", "codeapi"),
        "sub": user_id,
        "iat": now,
        "nbf": now,
        "exp": now + 60,
        "jti": uuid.uuid4().hex,
        "tenant_id": "legacy",
        "role": "USER",
        "principal_source": "librechat_jwt",
        "auth_context_hash": "test-minted-" + uuid.uuid4().hex,
    }
    return pyjwt.encode(
        claims, private_key_pem, algorithm="EdDSA",
        headers={"kid": env.get("CODEAPI_JWT_KID", "lc-codeapi-bundle-1")},
    )


_DOWNLOAD_PROBE_SCRIPT = """
(async () => {
  const res = await fetch("http://codeapi:3112/v1/download/%(session_id)s/%(file_id)s?kind=user", {
    headers: {"Authorization": "Bearer %(token)s"},
  });
  process.stdout.write(JSON.stringify({status: res.status, body: await res.text()}));
})();
"""


def _probe_download_as(chat_container: str, session_id: str, token: str) -> dict:
    # fileId must satisfy isValidId (service/src/utils.ts: 21-char nanoid shape,
    # /^[A-Za-z0-9_-]{21}$/) or sessionAuth 400s on shape before ever comparing
    # sessionKeys — a shape failure would read as a false negative for the isolation
    # check below, so this is a syntactically well-formed (if never-uploaded) id.
    file_id = "probeprobeprobeprobe1"
    assert len(file_id) == 21
    script = _DOWNLOAD_PROBE_SCRIPT % {
        "session_id": session_id, "file_id": file_id, "token": token,
    }
    result = subprocess.run(
        ["docker", "exec", chat_container, "node", "-e", script],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, (
        f"download probe script failed (rc={result.returncode}): "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    return json.loads(result.stdout)


class TestCrossUserSessionIsolation:
    """Challenge 2: "no test proves the cross-user clause [...] Log in as a second real
    user and prove A's artifacts are unreachable to B." Both users below are real,
    independently OIDC-authenticated Keycloak accounts driving real chat turns — user A
    is the same bootstrap DOGFOOD_USER test_correct_answer_the_model_cannot_predict_
    and_an_afterward_artifact already exercises; user B is provisioned fresh above.
    """

    def test_two_real_users_get_distinct_session_namespaces(
        self, chat_session, second_chat_session, chat_url, env,
    ):
        headers_a = _chat_headers(chat_session, chat_url)
        headers_b = _chat_headers(second_chat_session, chat_url)

        before = {ln for ln in compose(
            "exec", "-T", "codeapi-redis", "valkey-cli", "--no-auth-warning",
            "-a", env["CODEAPI_REDIS_PASSWORD"], "--scan", "--pattern", "session:*",
            check=True,
        ).stdout.splitlines() if ln.strip()}

        _run_hashing_turn(chat_session, chat_url, headers_a)
        new_after_a = _new_session_keys(env, before)
        assert new_after_a, "user A's real chat turn registered no new codeapi session"
        session_id_a, session_key_a = next(iter(new_after_a.items()))

        before_b = before | set(new_after_a)
        _run_hashing_turn(second_chat_session, chat_url, headers_b)
        new_after_b = _new_session_keys(env, before_b)
        assert new_after_b, "user B's real chat turn registered no new codeapi session"
        session_id_b, session_key_b = next(iter(new_after_b.items()))

        assert session_key_a != session_key_b, (
            f"two different real users produced the SAME codeapi sessionKey "
            f"({session_key_a!r}) — session-key.ts's per-user namespace "
            f"(<namespace>:user:<canonicalUserId>) is not actually separating them"
        )

        user_id_a = session_key_a.rsplit(":user:", 1)[-1]
        user_id_b = session_key_b.rsplit(":user:", 1)[-1]
        assert user_id_a and user_id_b and user_id_a != user_id_b, (
            f"expected distinct canonicalUserIds embedded in the two sessionKeys; got "
            f"{session_key_a!r} and {session_key_b!r}"
        )

    def test_users_output_session_is_unreachable_to_a_second_user(
        self, chat_session, second_chat_session, chat_url, env, chat_container,
    ):
        headers_a = _chat_headers(chat_session, chat_url)
        headers_b = _chat_headers(second_chat_session, chat_url)

        before = {ln for ln in compose(
            "exec", "-T", "codeapi-redis", "valkey-cli", "--no-auth-warning",
            "-a", env["CODEAPI_REDIS_PASSWORD"], "--scan", "--pattern", "session:*",
            check=True,
        ).stdout.splitlines() if ln.strip()}

        _run_hashing_turn(chat_session, chat_url, headers_a)
        new_keys = _new_session_keys(env, before)
        assert new_keys, "user A's real chat turn registered no new codeapi session"
        redis_key_a, session_key_a = next(iter(new_keys.items()))
        # `_new_session_keys` returns the full redis key (`session:<id>`); the URL path
        # param the /download route expects is just `<id>` (router.ts's
        # `/download/:session_id/:fileId`, looked up as `session:${session_id}` server
        # side) — using the prefixed form here would 400 on isValidId's shape check
        # before the isolation check under test ever runs.
        session_id_a = redis_key_a.removeprefix("session:")
        user_id_a = session_key_a.rsplit(":user:", 1)[-1]

        before_b = before | set(new_keys)
        _run_hashing_turn(second_chat_session, chat_url, headers_b)
        new_keys_b = _new_session_keys(env, before_b)
        assert new_keys_b, "user B's real chat turn registered no new codeapi session"
        _, session_key_b = next(iter(new_keys_b.items()))
        user_id_b = session_key_b.rsplit(":user:", 1)[-1]

        # Negative control: A, authenticated as A, hitting A's OWN session is not
        # blocked by sessionAuth (a 404 — no file with that id was ever uploaded — is
        # the expected miss; a 403 here would mean the endpoint always refuses and the
        # real assertion below would be meaningless).
        token_a = _mint_codeapi_bearer(env, user_id_a)
        as_a = _probe_download_as(chat_container, session_id_a, token_a)
        assert as_a["status"] != 403, (
            f"user A was refused their OWN session ({as_a}); the negative control below "
            f"is meaningless if this endpoint refuses everyone regardless of identity"
        )

        # The actual assertion: B, authenticated as B, hitting A's session must be
        # refused by codeapi's own sessionAuth (service/src/middleware/auth.ts) — A's
        # workspace has no path reachable to B.
        token_b = _mint_codeapi_bearer(env, user_id_b)
        as_b = _probe_download_as(chat_container, session_id_a, token_b)
        assert as_b["status"] == 403, (
            f"user B reached user A's codeapi output session ({as_b}) — Finding 27's "
            f"'no path to ... another user's workspace' clause is violated"
        )
        assert json.loads(as_b["body"]) == {"error": "Unauthorized"}, as_b
