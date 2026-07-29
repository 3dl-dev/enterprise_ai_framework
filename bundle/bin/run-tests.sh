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
# playwright drives the browser suite (make test-browser). Kept here so the venv is
# complete; the browser binaries are a separate `playwright install`.
"$VENV/bin/pip" install --quiet playwright

exec "$VENV/bin/pytest" tests/ -v --tb=short "$@"
