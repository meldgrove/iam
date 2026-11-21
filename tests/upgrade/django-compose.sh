#!/usr/bin/env bash
# The updater installs images, not files: a server of the Django version
# starts this version with its old compose.yaml. This starts 0.2.0 with that
# file, installs the image under test the way the updater's deploy script
# does (version in .env, docker compose up --wait), checks it, goes back to
# 0.2.0 (the deploy script's rollback), and installs the image again.
#
#   tests/upgrade/django-compose.sh <image>     e.g. iam:ci
#
# Needs Docker with Compose 2.20 or later, Node.js, and the host port
# UPGRADE_TEST_PORT (default 18000).
set -euo pipefail

image=${1:?Usage: tests/upgrade/django-compose.sh <image>}
port=${UPGRADE_TEST_PORT:-18000}
root=$(cd "$(dirname "$0")/../.." && pwd)
work=$(mktemp -d)
target=ghcr.io/meldgrove/iam:0.0.0-upgrade-test

cp "$root/tests/upgrade/compose.django.yaml" "$work/compose.yaml"
cp "$root/compose.postgres.yaml" "$work/compose.postgres.yaml"
cp "$root/tests/upgrade/compose.oidc.yaml" "$work/compose.oidc.yaml"
random() { node -e "process.stdout.write(require('crypto').randomBytes($1).toString('base64url'))"; }
password=$(random 24)
token=$(random 24)
# A .env of 0.2 (docs/deployment.md of 0.2.0), with the bundled PostgreSQL.
# Django allows origins with a wildcard; IAM's own one is the other entry.
cat > "$work/.env" << EOF
COMPOSE_FILE=compose.yaml:compose.postgres.yaml:compose.oidc.yaml
IAM_VERSION=0.2.0
IAM_PORT=$port
DJANGO_SECRET_KEY=$(random 48)
ENCRYPTION_KEY=$(random 32)=
ALLOWED_HOSTS=iam.example.invalid,localhost,127.0.0.1
CSRF_TRUSTED_ORIGINS=https://*.example.invalid/,https://iam.example.invalid
POSTGRES_PASSWORD=$password
DATABASE_URL=postgresql://iam:$password@postgres:5432/iam
HEALTHCHECK_TOKEN=$token
OIDC_ISSUER=http://sso.example.invalid:8080/realms/iam
OIDC_CLIENT_ID=iam
OIDC_CLIENT_SECRET=upgrade-test
EOF

# In the app's folder, as on a server: compose.yaml and .env from there.
compose() { (cd "$work" && docker compose --ansi never --project-name iam-upgrade-test "$@"); }
finish() {
  status=$?
  if [ "$status" -ne 0 ]; then compose logs --no-color --tail 80 migrate web worker || true; fi
  compose down --volumes --remove-orphans > /dev/null 2>&1 || true
  docker image rm "$target" > /dev/null 2>&1 || true
  rm -rf "$work"
  exit "$status"
}
trap finish EXIT
docker tag "$image" "$target"

# install <version>: what the deploy script does for a request.
install() {
  echo "Installing $1"
  sed -i "s/^IAM_VERSION=.*/IAM_VERSION=$1/" "$work/.env"
  compose up --detach --wait --wait-timeout 300 --quiet-pull
}
sql() { compose exec -T postgres psql -U iam -d iam -tAc "$1"; }
# A session of the Django version, which migrate ends.
django_session() {
  sql "insert into django_session (session_key, session_data, expire_date) values ('upgrade-test-$1', 'e30', now() + interval '1 day')" > /dev/null
}
# Django answers (through the proxy, which sets X-Forwarded-Proto).
django_runs() {
  curl -fsS -H "X-Forwarded-Proto: https" "http://127.0.0.1:$port/login/" | grep -q "Sign in to manage people and access."
}
checked() {
  # Healthy by the old file's Python health checks.
  for service in web worker; do
    test "$(docker inspect --format '{{.State.Health.Status}}' "$(compose ps --quiet "$service")")" = healthy
  done
  curl -fsS -H "accept: text/html" "http://127.0.0.1:$port/login" | grep -q '<div id="root">'
  test "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$port/api/v1/me")" = 401
  # Sign-in sends the provider back to IAM's own origin.
  location=$(curl -s -o /dev/null -w '%{redirect_url}' "http://127.0.0.1:$port/oidc/authenticate/")
  case "$location" in
    http://sso.example.invalid:8080/realms/iam/protocol/openid-connect/auth\?*redirect_uri=https%3A%2F%2Fiam.example.invalid%2Foidc%2Fcallback%2F*) ;;
    *) echo "Sign-in redirects to: $location" >&2 && return 1 ;;
  esac
  test "$(sql "select count(*) from django_session")" = 0
  test "$(sql "select count(*) from drizzle.__drizzle_migrations")" -gt 0
  # This worker reached the database (a beat after it started), and the web
  # app sees it; the 0.1.0 file checks the worker that way.
  started=$(docker inspect --format '{{.State.StartedAt}}' "$(compose ps --quiet worker)")
  beat=0
  for _ in $(seq 1 60); do
    beat=$(sql "select count(*) from iam_workerheartbeat where name = 'worker' and beat_at > '$started'")
    [ "$beat" = 1 ] && break
    sleep 1
  done
  test "$beat" = 1
  curl -fsS -H "X-Health-Token: $token" "http://127.0.0.1:$port/-/readyz" | grep -q '"worker":"ok"'
  compose exec -T worker python -c "import os,urllib.request; r=urllib.request.Request('http://web:8000/-/readyz',headers={'X-Health-Token':os.environ['HEALTHCHECK_TOKEN'],'X-Forwarded-Proto':'https'}); urllib.request.urlopen(r,timeout=5)"
}

install 0.2.0
django_runs
django_session 1

install "${target#*:}"
checked
echo "The image runs with the compose file of 0.2."

# The deploy script's rollback, after a later failure.
install 0.2.0
django_runs
django_session 2
echo "0.2.0 runs again on the database."

install "${target#*:}"
checked
echo "Installed again after the rollback."
