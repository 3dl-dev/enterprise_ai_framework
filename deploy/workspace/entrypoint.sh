#!/bin/bash
# Workspace entrypoint: seed the project on first boot, then serve a real shell.
#
# ttyd binds to loopback ONLY. It has no authentication of its own, and the shell it
# serves holds a virtual key that spends real money — anyone who can open the port owns
# both. The only thing that can reach it is the oauth2-proxy container in this same pod,
# which shares the network namespace and authenticates against Keycloak first. If you
# ever change `--interface lo` here, you have removed the entire access control.
set -euo pipefail

WS_USER="${WS_USER:-coder}"
PROJECT="${WS_PROJECT_DIR:-/workspace/project}"

if [[ ! -d "${PROJECT}/.git" ]]; then
    echo "==> seeding ${PROJECT}"
    mkdir -p "${PROJECT}"
    cd "${PROJECT}"
    git init -q -b main
    git config user.email "${WS_USER}@workspace.local"
    git config user.name "${WS_USER}"
    git config --global --add safe.directory "${PROJECT}" 2>/dev/null || true

    cat > README.md <<'EOF'
# Workspace

A real terminal, a real git repo, and a real `aider` wired to the gateway.

    aider                 # start the coding agent (your own virtual key, your own budget)
    make test             # run the tests
    /add app.py           # inside aider: put a file in the chat
    /help                 # inside aider: every slash command, because this is real aider

`app.py` has a deliberate bug: `add()` subtracts. `make test` fails until it is fixed.
EOF

    cat > app.py <<'EOF'
"""Tiny sample project. add() is deliberately wrong so there is something to fix."""


def add(a, b):
    return a - b


def greet(name):
    return f"hello, {name}"
EOF

    cat > test_app.py <<'EOF'
from app import add, greet


def test_add():
    assert add(2, 3) == 5


def test_greet():
    assert greet("world") == "hello, world"
EOF

    cat > Makefile <<'EOF'
.PHONY: test run

test:
	pytest -q

run:
	python -c "import app; print(app.add(2, 3))"
EOF

    git add -A
    git commit -q -m "Seed workspace"
    echo "==> seeded"
fi

cd "${PROJECT}"

exec /usr/local/bin/ttyd \
    --port 7681 \
    --interface lo \
    --writable \
    --ping-interval 30 \
    --client-option "titleFixed=workspace: ${WS_USER}" \
    --client-option "fontSize=14" \
    /usr/local/bin/workspace-shell
