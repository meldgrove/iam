# Deployment

One Docker image, `ghcr.io/meldgrove/iam`, runs three processes
from `compose.yaml`:

| Service | Command | Notes |
| --- | --- | --- |
| `migrate` | `manage.py migrate` | Runs once per deploy; web and worker wait for it. |
| `web` | gunicorn on port 8000 | Behind a reverse proxy that terminates HTTPS. |
| `worker` | `manage.py run_worker` | Runs jobs, the schedule and reads from connections. One is enough; with a second, one runs jobs while the other reads a big directory. |
| `updater` | Meldgrove Updater | Checks for and installs updates; the admins' update button talks to it ([updates.md](updates.md)). |

PostgreSQL 16 or newer runs separately (a managed database or a PostgreSQL
server of your own), or next to the app with `compose.postgres.yaml`.
Nothing else is needed: no Redis, no message queue. The stack is made for a
Linux server, such as a VPS with Debian or Ubuntu ([Setup](#setup)). To run
it on Coolify instead, see [Coolify](coolify.md); it uses
`compose.coolify.yaml`.

## Setup

On a server with Debian 12 or later, or a similar system such as Ubuntu
24.04, run the commands as root (`sudo -i`). Point a domain name at the
server first.

1. **Docker:** install Docker Engine and the Compose plugin from Docker's
   apt repository ([Debian](https://docs.docker.com/engine/install/debian/),
   [Ubuntu](https://docs.docker.com/engine/install/ubuntu/)), and Git:
   `apt install git`.
2. **Get the files** at the newest release:

   ```sh
   git clone --branch stable https://github.com/meldgrove/iam.git /srv/iam
   cd /srv/iam
   ```

3. **Create `.env`** next to `compose.yaml` with these variables, and make it
   readable for root only: `chmod 600 .env`. `.env.example` holds development
   settings; do not copy it.

   | Variable | Value |
   | --- | --- |
   | `IAM_VERSION` | `0.1` (a release series; with [automatic updates](updates.md), the server writes the exact version it installs here) |
   | `DJANGO_SECRET_KEY` | random, 50+ characters |
   | `ENCRYPTION_KEY` | `docker run --rm -e DJANGO_DEBUG=1 ghcr.io/meldgrove/iam:0.1 python manage.py generate_key`; keep a copy with the backups |
   | `DATABASE_URL` | your PostgreSQL, or the container below |
   | `ALLOWED_HOSTS` | `iam.example.invalid` |
   | `CSRF_TRUSTED_ORIGINS` | `https://iam.example.invalid` |
   | `HEALTHCHECK_TOKEN` | random |
   | `KEYCLOAK_URL`, `KEYCLOAK_REALM` or `OIDC_ISSUER` | the sign-in provider ([authentication.md](authentication.md)) |
   | `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET` | the sign-in client |
   | `BOOTSTRAP_ADMIN_EMAILS` or `OIDC_ADMIN_GROUPS` | the first admin |

4. **Database:** set `DATABASE_URL` to a PostgreSQL 16 or newer of your own
   that the containers can reach (`localhost` would be the container
   itself), or run one next to the app. For that, add to `.env`:

   ```sh
   COMPOSE_FILE=compose.yaml:compose.postgres.yaml
   POSTGRES_PASSWORD=<openssl rand -hex 32>
   DATABASE_URL=postgresql://iam:<the same password>@postgres:5432/iam
   ```

   With `COMPOSE_FILE` in `.env`, every `docker compose` command in
   `/srv/iam` uses both files. The data lives in the volume `postgres`.

5. **Start.** The second command adds the first admin; leave it out when
   `BOOTSTRAP_ADMIN_EMAILS` or `OIDC_ADMIN_GROUPS` makes one
   ([authentication.md](authentication.md#the-first-admin)).

   ```sh
   docker compose up -d --pull always
   docker compose run --rm web python manage.py createadmin --email admin@example.invalid
   ```

6. **HTTPS:** point the domain's DNS at the server, install Caddy
   (`apt install caddy`), and put this site into `/etc/caddy/Caddyfile`, in
   place of the package's example site `:80` and next to the sites of other
   apps:

   ```text
   iam.example.invalid {
   	reverse_proxy 127.0.0.1:8000
   }
   ```

   Then `systemctl reload caddy`. Caddy gets the certificate itself.
   `compose.yaml` publishes `web` on `127.0.0.1:8000` only; `IAM_PORT`
   changes the host port. With several apps on one server, give each its
   own port and its own site in the Caddyfile. Caddy's requests reach the
   container from the Docker network's gateway, which is why `compose.yaml`
   trusts forwarded headers from any address (`FORWARDED_ALLOW_IPS=*`):
   nothing outside the server reaches the port.

7. Sign in, then continue with "First steps" below.
8. **Automatic updates:** [switch them on](updates.md#switch-it-on).

## Health

- `GET /-/healthz`: the web process answers (liveness). The web
  healthcheck in `compose.yaml` uses it.
- `GET /-/readyz`: database reachable and worker heartbeat younger than
  `WORKER_HEARTBEAT_MAX_AGE_SECONDS`; 503 otherwise. With the header
  `X-Health-Token: <HEALTHCHECK_TOKEN>` it also lists each check and the
  version.
- The worker's healthcheck in `compose.yaml` checks a file its watchdog
  touches while the worker makes progress. A stuck worker ends itself and
  the restart policy starts it again.
- The dashboard warns when the worker has not reported in time.

Nothing in the app alerts anyone when the worker stops: point an uptime
monitor (Uptime Kuma, Better Stack, a cloud provider's checks) at
`https://iam.example.invalid/-/readyz`. It answers 503 when the database is
unreachable or the worker is stale, without the token too.

`compose.yaml` gives the worker two minutes to finish the job it is running
when a deploy stops it, and web 40 seconds to finish its requests.

## First steps after the first sign-in

1. **Admin › Connections:** add the directory (Keycloak, Google Workspace or a
   SCIM app), test it and import its groups. Add an email connection (SMTP)
   for initial passwords and task notifications. Leave connections off until
   the policy is ready; while a connection is off, changes there become tasks.
2. **Access profiles:** one per kind of person, with the accounts and groups
   they get. Mark the default profile per person type.
3. **Teams:** with the groups their members join.
4. **Accounts:** run "Compare accounts now" on each connection. It links
   existing accounts to people by work email or username and lists drift.
5. **People:** add them, or connect Personio to import them.
6. **Admin › Settings:** onboarding lead time, automatic onboarding and
   offboarding, the daily removal limit, who gets task emails, and the update
   channel ([updates.md](updates.md#channels)).
7. Turn the connections on.

## Trying it with real data: read-only mode

Start with `READ_ONLY=1` ([architecture.md](architecture.md#read-only-mode)).
The app then reads connected apps but never changes them and sends no email,
and every job only reports what it would change. A banner on every page
after sign-in shows that the mode is on.

1. Set `READ_ONLY=1` and deploy. Check `/-/readyz` with the health token:
   it answers `"read_only": true`.
2. Add the connections, turn them on, test them and import their groups.
   In read-only mode a connection that is on and automatic still only reads.
3. Build the access profiles and teams.
4. Import people. Existing staff arrive as planned; with automatic
   onboarding on (Admin › Settings), the schedule rehearses their onboarding
   within a minute and they become active in this app. Turn it off first to
   go person by person.
5. Compare accounts on each connection and review the drift. Jobs with the
   status "Checked only (read-only mode)" say what the app would do to each
   account: create, reactivate or disable it, and which groups it would add
   or remove. Fix the policy or the data and rehearse again with an access
   update.

To leave read-only mode:

1. Look at Jobs › Open. Queued jobs run for real once the mode is off, and
   so do held or failed jobs that someone approves or retries. Cancel the
   workflows you do not want.
2. Consider setting connections to manual first, then to automatic one at a
   time.
3. Set `READ_ONLY=0` and redeploy web and worker.
4. Rehearsed jobs are not replayed. Start an access update for each person
   whose access should now be applied, beginning with test accounts.

## Upgrades

Releases that need operator action list their steps under "Upgrade steps
needed" in the release notes ([releases.md](releases.md)); the update button
shows such a release as needing upgrade steps. Take the steps, then move to
the new series ([updates.md](updates.md#channels)): in `/srv/iam`, run
`git pull` (the newest release's `compose.yaml`), then install the release
with the update button ("Install anyway…"). Without the host setup: change
`IAM_VERSION` in `.env` and run `docker compose up -d --pull always`.

Servers set up before `compose.yaml` published the port have a
`compose.override.yaml` that publishes `127.0.0.1:8000:8000`. It does no
harm: Docker Compose merges the identical entry. To remove it after the
`git pull` that brings the new `compose.yaml`, delete it if it holds nothing
else, and run `docker compose up -d --force-recreate updater`: the host setup
runs Docker Compose with the files that started the updater, so it fails
while those name a deleted file.

Database migrations run automatically in `migrate`.
