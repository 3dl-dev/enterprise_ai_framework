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

# Projects, plural. A child makes several things across a camp, and the first version of
# this had exactly one project directory and one publish slot — so a second game silently
# destroyed the first. Each project is its own directory and its own git repo, and gets
# its own share link.
PROJECTS_ROOT="${WS_PROJECTS_ROOT:-/workspace/projects}"
ACTIVE_FILE="${PROJECTS_ROOT}/.active"

mkdir -p "${PROJECTS_ROOT}"
git config --global --add safe.directory '*' 2>/dev/null || true
git config --global user.email "${WS_USER}@workspace.local" 2>/dev/null || true
git config --global user.name "${WS_USER}" 2>/dev/null || true
git config --global init.defaultBranch main 2>/dev/null || true

# First boot: one empty project, deliberately EMPTY. The old seed was a Flask app with a
# broken add() — a test fixture. It anchored the agent on files nobody asked about, so
# "make me a game" came back asking to add app.py to the chat. Nothing to anchor on is
# strictly better than the wrong thing.
if [[ ! -s "${ACTIVE_FILE}" ]]; then
    FIRST="my-first-project"
    mkdir -p "${PROJECTS_ROOT}/${FIRST}"
    ( cd "${PROJECTS_ROOT}/${FIRST}" && git init -q -b main 2>/dev/null || true )
    printf '%s\n' "${FIRST}" > "${ACTIVE_FILE}"
    echo "==> created ${PROJECTS_ROOT}/${FIRST}"
fi

PROJECT="${PROJECTS_ROOT}/$(cat "${ACTIVE_FILE}")"
export WS_PROJECT_DIR="${PROJECT}"
mkdir -p "${PROJECT}"

cd "${PROJECT}"

# Client options, explained here because they cannot be commented inline — a `#` inside a
# backslash-continued command breaks the continuation and ttyd starts with no command.
#
#   scrollback=10000     a coding agent emits a lot of lines; the default is small enough
#                        that one long reply pushes the start of the answer out of history
#   scrollOnUserInput    keep the viewport at the bottom as output streams, otherwise a
#                        long reply leaves the view parked and reads as a frozen terminal
#   disableLeaveAlert    no "are you sure you want to leave" on a tab close
# The shell UI and the live preview. Loopback only; oauth2-proxy is the front door for
# every route in this pod.
/usr/local/bin/shell-server.py &

exec /usr/local/bin/ttyd \
    --port 7681 \
    --base-path /terminal \
    --interface lo \
    --writable \
    --ping-interval 30 \
    --client-option "titleFixed=workspace: ${WS_USER}" \
    --client-option "fontSize=14" \
    --client-option "scrollback=10000" \
    --client-option "scrollOnUserInput=true" \
    --client-option "disableLeaveAlert=true" \
    /usr/local/bin/workspace-shell
