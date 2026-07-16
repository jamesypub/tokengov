#!/usr/bin/env bash
# tg-env.sh — env resolver for parallel dev/stage installs
# on the same host. Sourced (not exec'd) by tg-local-* and
# tg-test-data-* scripts.
#
# Reads TG_ENV from the caller's environment and exports:
#   COMPOSE_PROJECT_NAME  → docker compose namespace
#   TG_COMPOSE_PROJECT    → mirror of above (for filter use)
#   TG_API_PORT           → host port for api (container :8000)
#   TG_PG_PORT            → host port for postgres (container :5432)
#   TG_ENV_FILE           → per-env compose env-file path
#
# Cases:
#   TG_ENV unset (default — public-facing INSTALL.md path):
#     project=tokengov  api=8000  pg=5432  env-file=.env.tg
#   TG_ENV=dev:
#     project=tg-dev    api=18000 pg=15432 env-file=.env.tg.dev
#   TG_ENV=stage:
#     project=tg-stage  api=28000 pg=25432 env-file=.env.tg.stage
#
# Anything else: fail. Public users never set TG_ENV; this is
# strictly internal tooling for running dev + stage in parallel
# (see issue #145).

case "${TG_ENV:-}" in
  "")
    export COMPOSE_PROJECT_NAME="tokengov"
    export TG_API_PORT="8000"
    export TG_PG_PORT="5432"
    export TG_ENV_FILE=".env.tg"
    ;;
  dev)
    export COMPOSE_PROJECT_NAME="tg-dev"
    export TG_API_PORT="18000"
    export TG_PG_PORT="15432"
    export TG_ENV_FILE=".env.tg.dev"
    ;;
  stage)
    export COMPOSE_PROJECT_NAME="tg-stage"
    export TG_API_PORT="28000"
    export TG_PG_PORT="25432"
    export TG_ENV_FILE=".env.tg.stage"
    ;;
  *)
    printf '\033[1;31m✗ unknown TG_ENV=%q (want dev|stage|unset)\033[0m\n' \
      "$TG_ENV" >&2
    return 1 2>/dev/null || exit 1
    ;;
esac

export TG_COMPOSE_PROJECT="$COMPOSE_PROJECT_NAME"
