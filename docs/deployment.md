# Deployment

One Docker image, `ghcr.io/meldgrove/iam`, runs three processes
from `compose.yaml`:

| Service | Command | Notes |
| --- | --- | --- |
| `migrate` | `migrate` | Applies the database migrations once per deploy; web and worker wait for it. Takes over a database of the Django version ([below](#upgrade-from-the-django-version)). |
| `web` | `web`: the server (Node.js) on port 8000 | The API and the web app, behind a reverse proxy that terminates HTTPS. |
| `worker` | `worker` | Runs jobs, the schedule and reads from connections. One is enough; with a second, one runs jobs while the other reads a big directory. |
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
   | `IAM_VERSION` | `0.3` (a release series; with [automatic updates](updates.md), the server writes the exact version it installs here) |
   | `APP_URL` | `https://iam.example.invalid` |
   | `AUTH_SECRET` | random, 32+ characters: `openssl rand -base64 48` |
   | `ENCRYPTION_KEY` | `docker run --rm ghcr.io/meldgrove/iam:0.3 cli generate-key`; keep a copy with the backups |
   | `DATABASE_URL` | your PostgreSQL, or the container below |
   | `HEALTHCHECK_TOKEN` | random |
   | `KEYCLOAK_URL`, `KEYCLOAK_REALM` or `OIDC_ISSUER` | the sign-in provider ([authentication.md](authentication.md)) |
   | `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET` | the sign-in client |
   | `BOOTSTRAP_ADMIN_EMAILS` or `OIDC_ADMIN_GROUPS` | the first admin |

4. **Database:** set `DATABASE_URL` to a PostgreSQL 16 or newer of your own
   that the containers can reach (`localhost` would be the container
   itself), or run one next to the app. For that, add to `.env`, with the
   same `openssl rand -hex 32` value in both places:

   ```sh
   COMPOSE_FILE=compose.yaml:compose.postgres.yaml
   POSTGRES_PASSWORD=REPLACE_WITH_RANDOM_HEX_PASSWORD
   DATABASE_URL=postgresql://iam:REPLACE_WITH_RANDOM_HEX_PASSWORD@postgres:5432/iam
   ```

   With `COMPOSE_FILE` in `.env`, every `docker compose` command in
   `/srv/iam` uses both files. The data lives in the volume `postgres`.

5. **Start.** The second command adds the first admin; leave it out when
   `BOOTSTRAP_ADMIN_EMAILS` or `OIDC_ADMIN_GROUPS` makes one
   ([authentication.md](authentication.md#the-first-admin)).

   ```sh
   docker compose up -d --pull always
   docker compose run --rm migrate cli create-admin --email admin@example.invalid
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
   own port and its own site in the Caddyfile. The app builds every link
   and redirect from `APP_URL`, not from forwarded headers.

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
4. **Accounts:** on each connection's page in Admin › Connections, run
   "Compare accounts now". It links existing accounts to people by work
   email or username and lists drift. An
   account whose username matches but whose email is someone else's stays
   unlinked.
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
   account: create, reactivate, disable or delete it, what else it would
   change there (such as the name or the sessions), and which groups it
   would add or remove. A job the connected app would refuse fails with the
   reason, as it would for real. Fix the policy or the data and rehearse again with an access
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

## Backups

### With Meldgrove Backups

The `backup-agent` service in `compose.yaml` sends the database to
[Meldgrove Backups](https://github.com/meldgrove/backups) when Backups asks.
While `BACKUP_URL` is empty, it idles.

1. In Backups, add a source of the kind **Meldgrove app**, and put the two
   lines it shows (`BACKUP_URL`, `BACKUP_TOKEN`) into `.env`.
2. Run `docker compose up -d`, then check the agent:
   `docker compose exec backup-agent backup-agent check`.
3. Let the backup end before the update window (`UPDATE_WINDOW`): automatic
   updates run migrations without anyone present.

Keep `ENCRYPTION_KEY` apart from the backups: a restore needs it, the backups
do not hold it, and Backups shows the name on the source page.

With [automatic updates](updates.md), run the updater's host setup again once
the app's folder has updater 0.2.2 or later (`git pull`): its deploy script
then pulls the agent's image with each update. Without it:
`docker compose pull backup-agent && docker compose up -d`.

To restore, follow the
[agent's guide](https://github.com/meldgrove/backup-agent#restore).

### By hand

[Automatic updates](updates.md#backups) describes backups by hand (`pg_dump`
from the `postgres` container) and where to keep `ENCRYPTION_KEY`.

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

## Upgrade from the Django version

Versions 0.1 and 0.2 of IAM were a Django app; 0.3 is the same app in
TypeScript (Node.js), with the same database, the same connections and the
same sign-in client. Its release is marked as needing upgrade steps, so
automatic updates do not install it.

1. **Back up the database** ([Backups](#backups)) and keep `ENCRYPTION_KEY`.
2. **Get the new files:** in `/srv/iam`, `git pull` (branch `stable`). The new
   `compose.yaml` starts the new processes and health checks; the Django
   one cannot run the new image.
3. **`.env` stays as it is.** `compose.yaml` takes `AUTH_SECRET` from
   `DJANGO_SECRET_KEY` and `APP_URL` from `CSRF_TRUSTED_ORIGINS` while those
   are set; rename them when convenient. `ENCRYPTION_KEY` keeps working: the
   saved connection secrets stay readable. `ALLOWED_HOSTS`,
   `TRUST_PROXY_HEADERS`, `FORWARDED_ALLOW_IPS`, `SECURE_SSL_REDIRECT`,
   `WEB_WORKERS`, `WEB_THREADS`, `DJANGO_DEBUG` and `DEPLOYMENT_ENV` are no
   longer read.
4. **Install it:** set `IAM_VERSION=0.3` in `.env`, then
   `docker compose up -d --pull always`. `migrate` takes over the database
   in one transaction: it keeps every table and row, adds the session table,
   and gives the foreign keys the rules on delete Django applied itself.
   Django's own tables stay, so going back works (step 6).
5. **Sign in again:** `migrate` ends the sessions of the Django version, at
   every start, so none survives for a rollback. The sign-in client
   needs no change: the callback stays `/oidc/callback/` and the address
   after sign-out `/login/`.
6. **Going back:** set `IAM_VERSION=0.2`, check out the release's files
   (`git checkout v0.2.0`), and run `docker compose up -d --pull always`.
   Sessions end again. Changes made in 0.3 stay in the database. At the next
   upgrade, `migrate` also ends the 0.3 sessions of everyone who signed in on
   0.2 in between.

An installation of 0.1 can go straight to 0.3: `migrate` adds the columns of
0.2 too.
