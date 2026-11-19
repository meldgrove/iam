#!/bin/sh
set -eu
# Log the request path only. Queries and referrers can carry OIDC parameters or personal data.
exec gunicorn config.wsgi:application \
  --bind 0.0.0.0:8000 \
  --workers "${WEB_WORKERS:-2}" \
  --threads "${WEB_THREADS:-4}" \
  --timeout 60 \
  --access-logfile - \
  --access-logformat '%(h)s %(l)s %(u)s %(t)s "%(m)s %(U)s %(H)s" %(s)s %(b)s "%(a)s"' \
  --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-127.0.0.1}"
