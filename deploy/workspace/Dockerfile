# The IDE surface: a real terminal in a browser, running a real terminal coding agent.
#
# The agent is OPENCODE (see the pin further down); aider is installed as a fallback and
# is one word away. The default swapped after aider was measured — it makes the user
# nominate files before it will act, and finding 23 caught it silently dropping a
# completed edit when the model named a file outside the chat.
#
# ttyd (MIT) serving an actual shell — not `aider --browser`, whose Streamlit GUI drops
# most slash commands, and not an MCP wrapper, whose one-shot `--message` throws away the
# context accumulation and the apply/lint/test/repair loop that is the whole product.
#
# Everything installed here is OSI-licensed: ttyd MIT, opencode MIT, aider Apache-2.0,
# the rest is Debian main. No component with a paid tier.
FROM python:3.12-slim

ARG TTYD_VERSION=1.7.7
# ttyd ships one static binary per architecture and nothing else; there is no multi-arch
# manifest to lean on, so the architecture is explicit and the build fails loudly if this
# image is ever built for something else.
ARG TTYD_ARCH=x86_64
ARG TTYD_SHA256=8a217c968aba172e0dbf3f34447218dc015bc4d5e59bf51db2f2cd12b7be4f55
ARG AIDER_VERSION=0.86.2

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl git make gcc g++ libc6-dev \
        jq less nano vim-tiny procps tini rsync unzip \
        fontconfig libfontconfig1 \
    && rm -rf /var/lib/apt/lists/*

# A self-contained container engine and a hypervisor, so a workspace can BUILD images and
# BOOT VMs the way any real dev box does. The forcing case is OVMX (`docker build -o dist`,
# then qemu boots the kernel), but this is deliberately generic: `docker` and `qemu` just
# work, for any tenant's project — nothing here is OVMX-specific.
#
# Everything is Debian main, so the header's "the rest is Debian main / no component with a
# paid tier" still holds: docker.io + docker-buildx are the Moby/BuildKit stack (Apache-2.0),
# qemu is GPL-2.0, gosu is MIT. docker-buildx (BuildKit) is what makes `docker build -o`
# local-export work; it is not optional for the OVMX build.
#
# THE DAEMON IS INERT BY DEFAULT. This image only INSTALLS the toolchain — entrypoint.sh
# starts dockerd solely when WS_DOCKER=1, which the pod sets only under the Sysbox runtime,
# where the container's root is user-namespace-mapped to an unprivileged host UID so a real
# in-pod dockerd is safe (enterpriseaiframework-ee4). In the default hardened pod (non-root,
# caps dropped, no Sysbox) these binaries are present but never started and nothing about
# the workspace changes. uidmap/slirp4netns/fuse-overlayfs are the rootless-docker path for
# nodes that never get Sysbox; iptables is dockerd's bridge-networking dependency.
RUN apt-get update && apt-get install -y --no-install-recommends \
        docker.io docker-cli docker-buildx \
        qemu-system-x86 qemu-utils \
        gosu uidmap iptables slirp4netns fuse-overlayfs \
    && rm -rf /var/lib/apt/lists/* \
    && dockerd --version && docker buildx version && qemu-system-x86_64 --version | head -1

RUN curl -fsSL -o /usr/local/bin/ttyd \
        "https://github.com/tsl0922/ttyd/releases/download/${TTYD_VERSION}/ttyd.${TTYD_ARCH}" \
    && echo "${TTYD_SHA256}  /usr/local/bin/ttyd" | sha256sum -c - \
    && chmod 0755 /usr/local/bin/ttyd \
    && /usr/local/bin/ttyd --version

# aider pulls a large dependency tree; pytest is here so the seeded project has a build
# command that actually runs, which is half the outcome this image exists for.
RUN pip install --no-cache-dir "aider-chat==${AIDER_VERSION}" pytest

# OpenCode (MIT) — an autonomous terminal agent, the Claude Code shape: it explores the
# repo itself rather than asking which files to add to the chat, which is the thing aider
# structurally will not do. Both are installed. Aider stays because it is measured against
# this gateway (see model-settings.yml) and a known-good fallback costs one binary.
#
# The standalone installer is used rather than npm so the image needs no Node runtime.
#
# PINNED, like ttyd and aider beside it. It was not, and that is worse here than for
# either of them: opencode's config loader hard-errors on any key it does not recognise
# and exits non-zero, so a schema change in a release we never chose turns the camp
# terminal into a stack trace on boot. Every config claim in this image — that
# `instructions` resolves, that the provider block is accepted — was verified against
# this exact version and is only true of it. Bump deliberately, then re-verify with
# `opencode debug config`.
ARG OPENCODE_VERSION=1.18.7
RUN curl -fsSL https://opencode.ai/install | VERSION="${OPENCODE_VERSION}" bash \
    && install -m 0755 /root/.opencode/bin/opencode /usr/local/bin/opencode \
    && rm -rf /root/.opencode \
    && /usr/local/bin/opencode --version | grep -qx "${OPENCODE_VERSION}"

# Runs as an unprivileged user. The pod additionally has no service-account token, no
# route to the API server and no route to any other workspace — but a container that
# starts as root is one CVE away from all of that mattering, so it does not start as root.
RUN useradd --create-home --uid 1000 --shell /bin/bash coder \
    && mkdir -p /workspace \
    && chown coder:coder /workspace

COPY model-settings.yml model-metadata.json /etc/aider/
# Global opencode config. NOTE: opencode validates this against a strict schema and
# rejects unknown keys, including a "_comment" field — so the reasoning lives here:
#
#   - baseURL/apiKey come from the pod env. The pod holds a per-user virtual key and no
#     provider credential, so this is the only route out and every request is metered,
#     budgeted and audited against the signed-in user.
#   - Limits are stated explicitly because the gateway presents these models as a generic
#     OpenAI endpoint; nothing upstream knows their real shape, and a client that assumes
#     a small context silently truncates history until the agent edits files it can no
#     longer see. Context is from the Forge catalogue. Output is the FULL provider budget,
#     not half: these models spend part of the same budget reasoning, so a short cap
#     truncates the answer after the thinking rather than shortening it.
#   - OPENCODE_CONFIG (exported by entrypoint.sh and workspace-shell) names this file
#     explicitly. That is belt-and-braces, NOT a repair: opencode 1.18.7 already reads
#     /etc/opencode/opencode.json on its own, verified by running `opencode debug config`
#     in the running pod with OPENCODE_CONFIG unset and getting the full provider and
#     model back. An earlier revision of this comment claimed the file "is never read at
#     all" without the variable; that was inferred from a laptop repro that did not use
#     this path, and it is wrong. The variable earns its place by covering the routes
#     that skip both scripts — `kubectl exec` being the one that matters.
#   - "instructions" names TWO files, loaded in order (enterpriseaiframework-cbf). Neither
#     lives in the project directory: a file in the project root gets read as work the
#     tenant's user asked for, gets published, and gets committed.
#
#       /etc/opencode/PLATFORM.md         baked into this image, COPYed below. Facts about
#                                          the pod, verified against the NetworkPolicy and
#                                          the live cluster, not preference — see that
#                                          file. Never tenant content, never a ConfigMap.
#       /etc/opencode/tenant/TENANT.md    NOT baked into this image. An operator's house
#                                          rules for THEIR deployment: a ConfigMap volume
#                                          (deploy/k8s/61-workspace.template.yaml, mounted
#                                          as a directory, not subPath, so a kubectl-side
#                                          content change reaches an already-running pod on
#                                          the kubelet's own sync — subPath mounts do NOT
#                                          get that sync, which is why this is a directory
#                                          and the file lives one level down). Seeded from
#                                          deploy/workspace/AGENTS.md by
#                                          deploy/bin/provision-workspace.sh the first time
#                                          a deployment provisions anyone, so the camp's
#                                          current rules are the default. `optional: true`
#                                          on that volume: a pod started before the
#                                          ConfigMap exists still starts, with an empty
#                                          tenant directory, rather than hanging in
#                                          ContainerCreating — verified against this exact
#                                          opencode build: `opencode debug config` with the
#                                          second instructions path pointed at a file that
#                                          does not exist still exits 0 and returns the
#                                          full resolved config, so a missing tenant file
#                                          is a missing tenant file, not a config error.
COPY opencode.json /etc/opencode/opencode.json
COPY PLATFORM.md /etc/opencode/PLATFORM.md
# install-godot / godot-web-export: the engine path. Not baked into the image itself —
# the export templates are a 1.3GB download and most projects never touch Godot — so the
# binary and templates install on demand onto the per-user PVC (see install-godot). These
# two scripts are the turnkey front door for "the agent can install its own dependencies":
# `install-godot` once, then `godot-web-export <project> <out>` per build.
COPY entrypoint.sh workspace-shell publish shell-server.py install-godot godot-web-export /usr/local/bin/
COPY shell/ /usr/local/bin/shell/
RUN chmod 0755 /usr/local/bin/entrypoint.sh /usr/local/bin/workspace-shell \
        /usr/local/bin/publish /usr/local/bin/shell-server.py \
        /usr/local/bin/install-godot /usr/local/bin/godot-web-export

USER coder
WORKDIR /workspace

# aider reads these as if they were the matching command-line flags. Held in the image
# rather than the manifest so every workspace is tuned identically; only the key, the
# model and the identity come from the pod.
# /workspace/.tools/bin is where install-godot drops the engine binary (on the PVC, so it
# survives restarts). Put it on PATH in the image itself, not just in entrypoint.sh, so a
# hand-typed `godot` also resolves under `kubectl exec`, which never runs the entrypoint.
ENV PATH=/workspace/.tools/bin:$PATH \
    HOME=/home/coder \
    AIDER_MODEL_SETTINGS_FILE=/etc/aider/model-settings.yml \
    AIDER_MODEL_METADATA_FILE=/etc/aider/model-metadata.json \
    AIDER_ANALYTICS_DISABLE=1 \
    AIDER_CHECK_UPDATE=false \
    AIDER_SHOW_MODEL_WARNINGS=false \
    AIDER_GITIGNORE=false \
    # Auto-confirm. The terminal drops straight into aider, so a confirmation prompt is
    # the first thing a user would meet and the least useful. Set in the image rather
    # than on the launch line so `aider` typed by hand behaves the same way.
    #
    # Worth being clear about what this trades: aider will edit files, create files and
    # run its own commands without asking. That is bounded by the pod — non-root, network
    # fenced, no cluster credential, its own PVC — and every change lands as a git commit
    # in the workspace repo, so `git log` and `git revert` are the undo. It is NOT bounded
    # for shell commands aider chooses to run inside the pod.
    AIDER_YES_ALWAYS=1 \
    # Without this the terminal fills with files nobody asked for. GLM habitually ends a
    # reply suggesting a command like `pytest test_app.py`; that trips aider's
    # file-mention heuristic, and because YES_ALWAYS auto-confirms, aider adds the file
    # and dumps its contents into the buffer. Measured during the edit-format probe as
    # the cause of every glm-5.2 diff failure — the suggestion also discarded the edit.
    AIDER_SUGGEST_SHELL_COMMANDS=false \
    # Same class: a URL in a reply becomes an auto-confirmed fetch-and-dump.
    AIDER_DETECT_URLS=false

EXPOSE 7681
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
