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

# Name the config explicitly. opencode 1.18.7 DOES find /etc/opencode/opencode.json by
# itself — checked by running `opencode debug config` in the running pod with this
# variable unset, which returns the full provider and model. (A previous version of this
# comment asserted the opposite and called it verified; the check behind it was run on a
# laptop against a different path. Corrected rather than deleted, because the wrong claim
# is the sort that gets defended later.)
#
# It is still set, for the paths that reach opencode without going through this script or
# workspace-shell — `kubectl exec` above all. It cannot live in $HOME/.config: /home/coder
# is an emptyDir and anything baked there is masked by the mount.
export OPENCODE_CONFIG=/etc/opencode/opencode.json

# Client options, explained here because they cannot be commented inline — a `#` inside a
# backslash-continued command breaks the continuation and ttyd starts with no command.
#
#   scrollback=10000     a coding agent emits a lot of lines; the default is small enough
#                        that one long reply pushes the start of the answer out of history
#   scrollOnUserInput    keep the viewport at the bottom as output streams, otherwise a
#                        long reply leaves the view parked and reads as a frozen terminal
#   disableLeaveAlert    no "are you sure you want to leave" on a tab close
#   theme                the terminal is not a widget on the page, it IS the page. Its
#                        background is byte-identical to --term-bg in shell/style.css
#                        (#0B0B0F) so there is no rectangle and no seam where the iframe
#                        starts. If either value ever changes, both change in the same
#                        commit. The rest of the palette is the same set of tokens the
#                        surrounding UI uses, so agent output does not read as a foreign
#                        program pasted into the page.
#   fontFamily           resolved by the BROWSER, not the image — xterm.js renders client
#                        side, so naming fonts installed in this container would be
#                        pointless. The stack is generic and ends in `monospace`, which
#                        every browser resolves to something.
#   lineHeight           1.35 matches the surrounding UI's rhythm; xterm's default 1.0 is
#                        noticeably tighter than the page around it.
#
# The `#` characters in the theme are why every value below is single-quoted: an unquoted
# `#` after whitespace starts a comment and silently eats the rest of the continued command,
# which is how ttyd once came up with no command at all.

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
    --client-option theme='{"background":"#0B0B0F","foreground":"#C9C7D4","cursor":"#FF6FA5","cursorAccent":"#0B0B0F","selectionBackground":"#2A2A34","selectionForeground":"#F2F1F6","black":"#1A1A21","red":"#FF5C7A","green":"#3DDC97","yellow":"#F5B045","blue":"#7AA2FF","magenta":"#FF6FA5","cyan":"#57D6DA","white":"#C9C7D4","brightBlack":"#63606F","brightRed":"#FF8AA0","brightGreen":"#7CEBBE","brightYellow":"#FFCB7D","brightBlue":"#A0BEFF","brightMagenta":"#FF9CC2","brightCyan":"#8AE7EA","brightWhite":"#F2F1F6"}' \
    --client-option 'fontFamily=ui-monospace, SF Mono, Cascadia Mono, Menlo, Consolas, monospace' \
    --client-option 'lineHeight=1.35' \
    /usr/local/bin/workspace-shell
