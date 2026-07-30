"""CLI: print one user's workspace-memory instructions markdown to stdout.

The control-plane pod is the only thing in this deployment with a network route to
LibreChat's Mongo and the credentials to read it (CHAT_MONGO_URL / CHAT_MONGO_DB, already
set on this container — see deploy/k8s/40-control-plane.yaml). The workspace pod itself
cannot reach Mongo (its NetworkPolicy allows only DNS, the gateway, and the MCP tool
servers — see deploy/k8s/60-workspace-common.yaml) and must not gain that route just to
read one file, so this is invoked from the OPERATOR's machine via
`kubectl exec deploy/control-plane -- python3 -m app.render_workspace_memory <user>`,
the same way deploy/bin/lib/tenant-instructions.sh drives a ConfigMap from a file on
disk — see deploy/bin/lib/workspace-memory.sh, which wires this into a
ConfigMap the workspace pod's own volume mount reads.

Usage: python3 -m app.render_workspace_memory <username>
"""

import sys

from app import chat_memory


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: render_workspace_memory.py <username>", file=sys.stderr)
        return 2
    sys.stdout.write(chat_memory.render_instructions_markdown(argv[1]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
