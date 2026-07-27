#!/usr/bin/env bash
# Block until every service with a healthcheck reports healthy, or fail loudly.
#
# `docker compose up -d` returns as soon as containers are created, not when they work.
# Without this the post-up steps race Keycloak's first-boot realm import, which takes
# the better part of a minute.
set -euo pipefail

cd "$(dirname "$0")/.."
COMPOSE=(docker compose -f docker-compose.yml --env-file .env)

TIMEOUT="${WAIT_TIMEOUT:-300}"
deadline=$(( SECONDS + TIMEOUT ))

echo "waiting for services to become healthy (timeout ${TIMEOUT}s)"

while :; do
    unhealthy=""
    while read -r name state health; do
        [[ -z "$name" ]] && continue
        # Services without a healthcheck report an empty health field; running is enough.
        if [[ -n "$health" && "$health" != "healthy" ]]; then
            unhealthy+=" ${name}(${health})"
        elif [[ -z "$health" && "$state" != "running" ]]; then
            unhealthy+=" ${name}(${state})"
        fi
    done < <("${COMPOSE[@]}" ps --format '{{.Service}} {{.State}} {{.Health}}')

    if [[ -z "$unhealthy" ]]; then
        echo "all services healthy"
        exit 0
    fi

    if (( SECONDS >= deadline )); then
        echo "timed out waiting for:${unhealthy}" >&2
        echo "--- recent logs ---" >&2
        "${COMPOSE[@]}" logs --tail=40 >&2
        exit 1
    fi

    echo "  still waiting:${unhealthy}"
    sleep 5
done
