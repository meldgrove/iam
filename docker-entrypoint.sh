#!/bin/sh
set -e
case "$1" in
  all|web|worker|migrate)
    export APP_ROLE="$1"
    shift
    # instrument.mjs starts Sentry before the app loads (tracing needs that).
    exec node --import /app/apps/server/dist/instrument.mjs /app/apps/server/dist/main.mjs "$@"
    ;;
  *)
    exec "$@"
    ;;
esac
