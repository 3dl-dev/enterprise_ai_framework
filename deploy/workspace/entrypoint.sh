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

# ---------------------------------------------------------------- self-contained docker
# A real dockerd inside the workspace, so `docker build` / `docker run` / qemu just work
# (enterpriseaiframework-ee4). This runs ONLY when the pod opts in with WS_DOCKER=1 AND we
# started as container-root — which happens exclusively under the Sysbox runtime, where
# that root is user-namespace-mapped to an unprivileged host UID. When WS_DOCKER is unset
# (every default hardened pod) this whole block is skipped and the entrypoint behaves
# exactly as it always has: no daemon, shell as the unprivileged user.
#
# The shell must NEVER run as root — docker or not — so we start dockerd as root and then
# re-exec ourselves as `coder` via gosu. WS_DROPPED guards the re-exec so dockerd is not
# started twice, and it means the project seeding below always runs as coder (files stay
# coder-owned on the PVC), identical to the non-docker path.
if [[ "${WS_DOCKER:-}" == "1" && "$(id -u)" == "0" && -z "${WS_DROPPED:-}" ]]; then
    echo "==> WS_DOCKER=1: starting self-contained dockerd"
    # data-root is where images/layers live; a build can be large, so the pod is expected
    # to back this path with a sized volume (see the Sysbox pod variant). Default keeps it
    # off the small project PVC.
    DOCKER_DATA_ROOT="${WS_DOCKER_DATA_ROOT:-/var/lib/docker}"
    mkdir -p "${DOCKER_DATA_ROOT}"
    # dockerd's own output goes to a log, never the user's terminal.
    dockerd --host=unix:///var/run/docker.sock --data-root="${DOCKER_DATA_ROOT}" \
        >/var/log/dockerd.log 2>&1 &
    for _ in $(seq 1 30); do [[ -S /var/run/docker.sock ]] && break; sleep 1; done
    if [[ -S /var/run/docker.sock ]]; then
        # Let coder use the socket without being root.
        chgrp "${WS_USER}" /var/run/docker.sock 2>/dev/null || true
        chmod 0660 /var/run/docker.sock 2>/dev/null || true
        echo "==> dockerd ready"
    else
        # Not fatal: the shell must still come up. docker just won't be there, which is a
        # far better failure than a workspace that never starts.
        echo "!! dockerd did not create its socket in time; see /var/log/dockerd.log" >&2
    fi
    export WS_DROPPED=1
    export DOCKER_HOST="unix:///var/run/docker.sock"
    exec gosu "${WS_USER}" "$0" "$@"
fi

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
#
# A second, unrelated trap lives in the same flags: ttyd's OWN option parser truncates a
# --client-option VALUE at a second '=', silently. This is server-side C, not the browser —
# src/server.c (ttyd 1.7.7, the version pinned in Dockerfile's TTYD_VERSION), case 't', calls
# `strsep(&option, "=")` TWICE: once to split the key off the front of "key=value...", and
# again on whatever strsep left behind to split the value off THAT. Whatever comes after the
# second '=' is simply gone — not appended to the value, not an error, no output at all.
# Example: --client-option "titleFixed=user=admin" parses as key "titleFixed", value "user";
# "admin" vanishes. Verified against the actual pinned binary (sha256 matches Dockerfile's
# TTYD_SHA256), not just the source: started it with that flag and read the SET_PREFERENCES
# frame it sends the client over its websocket protocol — it came back
# `{"titleFixed": "user", "fontSize": 14}`. See tests/test_ttyd_client_options.py, which
# reimplements the same two-strsep logic and pins it to that observed value.
# None of the values below contain a second '=' as written, so none are truncated today.
# titleFixed is the one to watch — it interpolates ${WS_USER} at runtime, and a WS_USER value
# that itself contained '=' would trip this exactly the same way.

# THE BIND, AND WHY IT IS NO LONGER LOOPBACK
#
# This used to be `--interface lo`, with the comment that widening it "removes the entire
# access control" — true at the time, because the oauth2-proxy sidecar sharing this
# network namespace was the only guard.
#
# The portal now serves the workshop as a tab on one origin, which means the control
# plane has to reach these ports from another pod, which means loopback is no longer
# possible. That is a real reduction in defence and is not pretended otherwise. Two
# independent controls replace the one:
#
#   1. A NetworkPolicy that permits ingress on 7681/7682 ONLY from the control-plane pod
#      (deploy/k8s/60-workspace-common.yaml). Note the CNI caveat recorded there: this
#      cluster's kube-router allows a packet as soon as the DESTINATION's ingress rules
#      permit it, without consulting the SOURCE's egress rules — so the `from` list is
#      what actually closes workspace-to-workspace traffic, and it is tested, not assumed.
#   2. A credential on ttyd itself and a token on the shell server, so reaching the port
#      is not the same as using it. Pod-local, and it holds even if the policy does not.
#
# If WS_INTERNAL_TOKEN is absent we refuse to start rather than come up unauthenticated:
# a workspace that silently loses its second lock is exactly the failure nobody notices.
if [[ -z "${WS_INTERNAL_TOKEN:-}" ]]; then
    echo "refusing to start: WS_INTERNAL_TOKEN is not set." >&2
    echo "  It is the credential the control plane presents to this pod. Without it the" >&2
    echo "  terminal would be reachable by anything the NetworkPolicy failed to stop." >&2
    exit 1
fi

# The shell UI and the live preview. Reachable from the control plane only; it checks the
# same token on every request.
/usr/local/bin/shell-server.py &

exec /usr/local/bin/ttyd \
    --port 7681 \
    --base-path /terminal \
    --credential "ws:${WS_INTERNAL_TOKEN}" \
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
