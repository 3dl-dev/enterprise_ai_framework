#!/usr/bin/env bash
# Run the scope-item test suite against the running bundle.
#
# These tests are the adjudication evidence for the sealed estimate, so they run against
# the real stack. A venv is created on first use; it is gitignored.
set -euo pipefail

cd "$(dirname "$0")/../.."

VENV=.venv-test

if [[ ! -d "$VENV" ]]; then
    echo "creating test venv"
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install --quiet --upgrade pip
fi

# Installed on EVERY run, not only when the venv is created. Adding a dependency here used
# to take effect for nobody who already had a venv: the block above was guarded by
# `if [[ ! -d "$VENV" ]]`, so an existing .venv-test never saw the new package and the
# suite died at collection with ModuleNotFoundError. It worked on a fresh clone and in CI,
# which is what made it nasty — the person adding the dependency saw green and everyone
# else saw an import error. pyyaml was added with enterpriseaiframework-cbf and hit exactly
# that; test_published_layout.py then failed to import on an existing checkout.
# pip is a no-op in about a second when the requirements are already satisfied.
"$VENV/bin/pip" install --quiet pytest==8.4.2 httpx==0.28.1 pyyaml==6.0.2
# fastapi (pulling in starlette) for control-plane/tests/test_portal_auth.py's TestClient.
# Pinned to the version control-plane/requirements.txt already runs, so the test venv
# exercises the same fastapi/starlette Request.client behaviour the service itself does.
# Deliberately NOT control-plane/requirements.txt wholesale: that also pulls asyncpg and
# pymongo for a live database the auth test stubs out rather than starts.
"$VENV/bin/pip" install --quiet fastapi==0.118.0
# websockets, pinned to the version control-plane/requirements.txt runs, for
# control-plane/tests/test_agent_console.py. The agent console's terminal panel is a
# websocket and app/agent_console.py bridges it with this library; the test stands a real
# websocket server up on loopback and drives the bridge through it, so a stub would be
# mocking the transport that is under test.
"$VENV/bin/pip" install --quiet websockets==13.1
# uvicorn, pinned the same way, because two of that file's claims cannot be made through
# TestClient: its ASGI transport collects a whole response body before returning it (so a
# proxy that buffered an event stream would look streamed) and an in-process call has no
# socket to close (so a "disconnect" would not be one). Those tests run the app behind a
# real server on loopback instead.
"$VENV/bin/pip" install --quiet uvicorn==0.37.0
# PyJWT[crypto] (pulls in `cryptography`) for tests/test_code_execution.py's cross-user
# isolation probe: it mints its own EdDSA codeapi bearer JWTs with the SAME signing key
# and claim shape LibreChat's packages/api/src/auth/codeapi.ts uses, so it can drive
# codeapi's session-key isolation logic directly for two genuine, already-authenticated
# users without needing to intercept LibreChat's own per-request token minting.
"$VENV/bin/pip" install --quiet "pyjwt[crypto]==2.12.1"
# playwright drives the browser suite (make test-browser). Kept here so the venv is
# complete; the browser binaries are a separate `playwright install`.
"$VENV/bin/pip" install --quiet playwright

# pymongo, pinned to the same version control-plane/requirements.txt runs, for
# tests/test_workspace_memory_bridge.py (enterpriseaiframework-471). Unlike
# test_portal_auth.py's stubbed-out asyncpg/pymongo above, this suite's whole point is
# proving real query semantics (collection name, ObjectId matching, field shapes)
# against a real, disposable mongod -- a stub of the driver would be asserting the
# thing that is actually under test, not a nicety to skip installing.
"$VENV/bin/pip" install --quiet pymongo==4.10.1

# asyncpg, pinned to the same version control-plane/requirements.txt runs, for
# control-plane/tests/test_metering_continuous_history.py (enterpriseaiframework-730). Its
# whole point is proving app/metering.py's real SQL (the LiteLLM pre-flip half of the
# continuous-history merge) against a real, disposable Postgres container -- same rationale
# as pymongo above, and unlike test_export_attribution.py/test_portal_auth.py, which stub
# asyncpg out because their subject is attribution wiring, not SQL correctness. The suite
# itself skips (not fails) when docker is unavailable.
"$VENV/bin/pip" install --quiet asyncpg==0.30.0

# control-plane/tests/ is not under tests/ and pytest.ini's testpaths is overridden by any
# explicit path given here, so it must be listed alongside tests/ or it silently never
# runs — which is exactly how it sat green-by-never-executing before this line existed.
exec "$VENV/bin/pytest" tests/ control-plane/tests/ -v --tb=short "$@"
