# The IDE surface: a real terminal in a browser, running real aider.
#
# ttyd (MIT) serving an actual shell — not `aider --browser`, whose Streamlit GUI drops
# most slash commands, and not an MCP wrapper, whose one-shot `--message` throws away the
# context accumulation and the apply/lint/test/repair loop that is the whole product.
#
# Everything installed here is OSI-licensed: ttyd MIT, aider Apache-2.0, the rest is
# Debian main. No component with a paid tier.
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
        jq less nano vim-tiny procps tini \
    && rm -rf /var/lib/apt/lists/*

RUN curl -fsSL -o /usr/local/bin/ttyd \
        "https://github.com/tsl0922/ttyd/releases/download/${TTYD_VERSION}/ttyd.${TTYD_ARCH}" \
    && echo "${TTYD_SHA256}  /usr/local/bin/ttyd" | sha256sum -c - \
    && chmod 0755 /usr/local/bin/ttyd \
    && /usr/local/bin/ttyd --version

# aider pulls a large dependency tree; pytest is here so the seeded project has a build
# command that actually runs, which is half the outcome this image exists for.
RUN pip install --no-cache-dir "aider-chat==${AIDER_VERSION}" pytest

# Runs as an unprivileged user. The pod additionally has no service-account token, no
# route to the API server and no route to any other workspace — but a container that
# starts as root is one CVE away from all of that mattering, so it does not start as root.
RUN useradd --create-home --uid 1000 --shell /bin/bash coder \
    && mkdir -p /workspace \
    && chown coder:coder /workspace

COPY model-settings.yml model-metadata.json /etc/aider/
COPY entrypoint.sh workspace-shell /usr/local/bin/
RUN chmod 0755 /usr/local/bin/entrypoint.sh /usr/local/bin/workspace-shell

USER coder
WORKDIR /workspace

# aider reads these as if they were the matching command-line flags. Held in the image
# rather than the manifest so every workspace is tuned identically; only the key, the
# model and the identity come from the pod.
ENV HOME=/home/coder \
    AIDER_MODEL_SETTINGS_FILE=/etc/aider/model-settings.yml \
    AIDER_MODEL_METADATA_FILE=/etc/aider/model-metadata.json \
    AIDER_ANALYTICS_DISABLE=1 \
    AIDER_CHECK_UPDATE=false \
    AIDER_SHOW_MODEL_WARNINGS=false \
    AIDER_GITIGNORE=false

EXPOSE 7681
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
