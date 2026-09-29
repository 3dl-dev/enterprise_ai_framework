# The EAF hosted-mode Raven image (agents-raven.md, Contract E). A thin layer over the
# UPSTREAM Raven image; nothing of Raven is reimplemented.
#
#   deploy/bin/kaniko-build.sh deploy/raven <registry>/raven-hosted:<tag> \
#       --build-arg RAVEN_BASE=<registry>/raven-upstream:<upstream-tag>
#
# The upstream image itself is built by kaniko from a PINNED upstream tag (its own
# Dockerfile, no edits) via a remote context; see deploy/raven/README.md for the exact
# command and the pinned commit. Two images rather than one because the upstream source
# tree (~240MB) is far past kaniko-build.sh's ConfigMap directory-context cap.
#
# What this layer does, and why each item is here rather than in a manifest:
#
#   * Curator and Evolver are ABSENT, not merely switched off. Upstream's Dockerfile does
#     `COPY . /app`, which ships /app/experimental/curator (the self-modifying harness
#     writer, "ships with the repository rather than the installed package"; verified
#     present in the v0.2.3 image). /app/evolver (the offline harness rewriter) was NOT in
#     the v0.2.3 image when measured; it is removed anyway so a future tag cannot add it
#     silently. A hosted agent must not be able to rewrite its own harness.
#   * The WebUI is reachable from the console proxy. Upstream's engine binds 127.0.0.1
#     only; the image fronts it with nginx on :80. nginx is retargeted to :18793 (the port
#     Contract E and 66-agent-console-common.yaml name). The engine stays on loopback.
#   * EverMind outbound is off by default (env below) and is additionally denied at the
#     network by a NetworkPolicy; see deploy/raven/hosted_seed.py for the config side.
#   * A boot wrapper enforces the hosted invariants in config.json on EVERY boot before
#     the upstream entrypoint runs, so a WebUI edit cannot turn them back on.
ARG RAVEN_BASE
FROM ${RAVEN_BASE}

# Curator + Evolver: not installed (repo-only trees upstream; see above).
RUN rm -rf /app/experimental/curator /app/evolver \
    && ! test -e /app/experimental/curator && ! test -e /app/evolver

# WebUI port. Rewritten in the shipped template (not via envsubst, whose variable list is
# fixed by the upstream entrypoint), and the build fails if the substitution did not land.
RUN sed -i 's/^\(\s*\)listen 80;/\1listen 18793;/' /etc/nginx/templates/raven.conf.template \
    && grep -q 'listen 18793;' /etc/nginx/templates/raven.conf.template

COPY hosted_seed.py /opt/eaf/hosted_seed.py
COPY hosted-entrypoint.sh /opt/eaf/hosted-entrypoint.sh
RUN chmod +x /opt/eaf/hosted-entrypoint.sh

# Hosted-mode switches. These are DEFAULTS baked into the image so a pod that forgets to
# set them still starts hosted; the seed script re-asserts the config-file side each boot.
#   RAVEN_HOSTED=1              the marker hosted_seed.py and the tests key on
#   RAVEN_AUTO_LOGIN=0          nginx does not hand every visitor the session cookie; the
#                               console proxy presents X-Raven-Token (RAVEN_SERVE_TOKEN,
#                               set per pod from the -key Secret) instead
#   RAVEN_NO_UPDATE_CHECK=1     no upstream release lookup (the update notice)
#   RAVEN_SKILLHUB_URL          upstream's DEFAULT hub is https://skillhub.evermind.ai and
#                               the engine refuses non-https / local overrides; an https
#                               name under the reserved .invalid TLD can never resolve, so
#                               the hub is dead by construction, not by absence of a click
#   EVEROS_API__PORT            EverOS memory listens on loopback :18791 (design: on the PVC)
#   RAVEN_WEB_PORT/PAGE_PORT    the port the entrypoint announces / the engine's loopback port
ENV RAVEN_HOSTED=1 \
    RAVEN_AUTO_LOGIN=0 \
    RAVEN_NO_UPDATE_CHECK=1 \
    RAVEN_SKILLHUB_URL=https://skillhub.eaf-disabled.invalid \
    EVEROS_API__PORT=18791 \
    RAVEN_WEB_PORT=18793

EXPOSE 18793
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/eaf/hosted-entrypoint.sh"]
CMD ["run"]
