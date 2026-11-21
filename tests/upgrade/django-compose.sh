#!/usr/bin/env bash
# The updater installs images, not files: a server of the Django version
# starts this version with its old compose.yaml. This starts 0.2.0 with that
# file, installs the image under test the way the updater's deploy script
# does (version in .env, docker compose up --wait), checks it, goes back to
# 0.2.0 (the deploy script's rollback), and installs the image again.
#
#   tests/upgrade/django-compose.sh <image>     e.g. iam:ci
#
# Needs Docker with Compose 2.20 or later, and the host port UPGRADE_TEST_PORT
# (default 18000).
set -euo pipefail

image=${1:?Usage: tests/upgrade/django-compose.sh <image>}
port=${UPGRADE_TEST_PORT:-18000}
root=$(cd "$(dirname "$0")/../.." && pwd)
work=$(mktemp -d)
target=ghcr.io/meldgrove/iam:0.0.0-upgrade-test

cp "$root/tests/upgrade/compose.django.yaml" "$work/compose.yaml"
cp "$root/compose.postgres.yaml" "$work/compose.postgres.yaml"
random() { node -e "process.stdout.write(require('crypto').randomBytes($1).toString('base64url'))"; }
password=$(random 24)
# A .env of 0.2 (docs/deployment.md of 0.2.0), with the bundled PostgreSQL.
cat > "$work/.env" << EOF
COMPOSE_FILE=compose.yaml:compose.postgres.yaml
IAM_VERSION=0.2.0
IAM_PORT=$port
DJANGO_SECRET_KEY=$(random 48)
ENCRYPTION_KEY=$(random 32)=
ALLOWED_HOSTS=iam.example.invalid,localhost,127.0.0.1
CSRF_TRUSTED_ORIGINS=https://iam.example.invalid
POSTGRES_PASSWORD=$password
DATABASE_URL=postgresql://iam:$password@postgres:5432/iam
HEALTHCHECK_TOKEN=$(random 24)
OIDC_ISSUER=https://sso.example.invalid/realms/iam
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
checked() {
  # Healthy by the old file's Python health checks.
  for service in web worker; do
    test "$(docker inspect --format '{{.State.Health.Status}}' "$(compose ps --quiet "$service")")" = healthy
  done
  curl -fsS -H "accept: text/html" "http://127.0.0.1:$port/login" | grep -q '<div id="root">'
  test "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$port/api/v1/me")" = 401
  test "$(sql "select count(*) from django_session")" = 0
  test "$(sql "select count(*) from drizzle.__drizzle_migrations")" -gt 0
}

install 0.2.0
curl -fsS "http://127.0.0.1:$port/-/healthz" > /dev/null
django_session 1

install "${target#*:}"
checked
echo "The image runs with the compose file of 0.2."

# The deploy script's rollback, after a later failure.
install 0.2.0
curl -fsS "http://127.0.0.1:$port/login/" > /dev/null
django_session 2
echo "0.2.0 runs again on the database."

install "${target#*:}"
checked
echo "Installed again after the rollback."
