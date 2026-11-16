#!/bin/sh
set -eu
exec gunicorn config.wsgi:application \
  --bind 0.0.0.0:8000 \
  --workers "${WEB_WORKERS:-2}" \
  --threads "${WEB_THREADS:-4}" \
  --timeout 60 \
  --access-logfile - \
  --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-127.0.0.1}"
