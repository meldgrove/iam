# Deployment

One Docker image, `ghcr.io/meldgrove/iam`, runs three processes
from `compose.yaml`:

| Service | Command | Notes |
| --- | --- | --- |
| `migrate` | `manage.py migrate` | Runs once per deploy; web and worker wait for it. |
| `web` | gunicorn on port 8000 | Behind a reverse proxy that terminates HTTPS. |
| `worker` | `manage.py run_worker` | Runs jobs and the schedule. Exactly one is enough; more are safe. |
| `updater` | Meldgrove Updater | Installs new releases ([updates.md](updates.md)). |

PostgreSQL 16 or newer runs separately (a managed database, or its own Coolify
resource). Nothing else is needed: no Redis, no message queue.

## Coolify

1. Create a PostgreSQL resource and note its internal URL.
2. Create a Docker Compose resource from this repository, branch `stable`
   (it always holds the `compose.yaml` of the newest release). Under
   Advanced, set Auto deploy to "Manual deployments only" so only the updater
   deploys. Turn on "Connect To Predefined Network" to reach the database.
3. Assign the domain to `web`, port 8000.
4. Set the environment variables:

   | Variable | Value |
   | --- | --- |
   | `IAM_VERSION` | `0.1` (a release series; see [updates.md](updates.md)) |
   | `DJANGO_SECRET_KEY` | random, 50+ characters |
   | `ENCRYPTION_KEY` | `docker run --rm ghcr.io/meldgrove/iam:0.1 python manage.py generate_key`; keep a copy with the backups |
   | `DATABASE_URL` | the PostgreSQL resource's internal URL |
   | `ALLOWED_HOSTS` | `iam.example.invalid` |
   | `CSRF_TRUSTED_ORIGINS` | `https://iam.example.invalid` |
   | `HEALTHCHECK_TOKEN` | random |
   | `KEYCLOAK_URL`, `KEYCLOAK_REALM` or `OIDC_ISSUER` | the sign-in provider ([authentication.md](authentication.md)) |
   | `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET` | the sign-in client |
   | `BOOTSTRAP_ADMIN_EMAILS` or `OIDC_ADMIN_GROUPS` | the first admin |

5. Deploy. Sign in, then continue with "First steps" below.

## Docker Compose without a platform

Put the variables above in `.env` next to `compose.yaml`, run a reverse proxy
(Caddy, Traefik, nginx) in front of `web`, and start:

```sh
docker compose up -d
docker compose run --rm web python manage.py createadmin --email admin@example.invalid
```

## Health

- `GET /-/healthz`: the web process answers (liveness).
- `GET /-/readyz`: database reachable and worker heartbeat younger than
  `WORKER_HEARTBEAT_MAX_AGE_SECONDS`; 503 otherwise. With the header
  `X-Health-Token: <HEALTHCHECK_TOKEN>` it also lists each check and the
  version. The worker's healthcheck in `compose.yaml` uses it.
- The dashboard warns when the worker has not reported in time.

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
   offboarding, the daily removal limit, and who gets task emails.
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
needed" in the release notes ([releases.md](releases.md)). Database
migrations run automatically in `migrate`.
