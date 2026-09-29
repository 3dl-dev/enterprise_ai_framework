#!/usr/bin/env sh
# Hosted-mode wrapper around the upstream Raven entrypoint. `run` (the default) enforces the
# hosted invariants in config.json first; every other verb is passed straight through so
# `docker-entrypoint.sh signin` and `raven ...` keep working for an operator exec.
set -eu

case "${1:-run}" in
    run)
        python /opt/eaf/hosted_seed.py
        ;;
esac
exec /usr/local/bin/docker-entrypoint.sh "$@"
