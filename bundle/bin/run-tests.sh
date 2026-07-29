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
    "$VENV/bin/pip" install --quiet pytest==8.4.2 httpx==0.28.1 pyyaml==6.0.2
    # playwright drives the browser suite (make test-browser). Installed here so the
    # venv is complete; the browser binaries are a separate `playwright install`.
    "$VENV/bin/pip" install --quiet playwright
fi

exec "$VENV/bin/pytest" tests/ -v --tb=short "$@"
