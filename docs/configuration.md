# Configuration

Everything an admin changes while the app runs lives in the admin section:
connections and their credentials (Admin › Connections), app users and roles
(Admin › Users and roles), lifecycle rules and notifications (Admin ›
Settings). Environment variables hold only what the process needs before it
can read the database, and the sign-in settings.

## Required in production

| Variable | Meaning |
| --- | --- |
| `DJANGO_SECRET_KEY` | Random value, at least 50 characters. Signs sessions. |
| `ENCRYPTION_KEY` | Fernet key that encrypts connection secrets. Create one with `python manage.py generate_key`. Several comma-separated keys: the first encrypts, all decrypt (rotation below). Losing it makes the saved connection secrets unreadable. |
| `DATABASE_URL` | PostgreSQL, e.g. `postgresql://iam:password@postgres:5432/iam`. Connecting gives up after 10 seconds, and TCP keepalives notice a server that went away within about a minute; `?connect_timeout=...` and the `keepalives_*` parameters in the URL override that. |
| `ALLOWED_HOSTS` | Public hostname(s), comma-separated. |
| `CSRF_TRUSTED_ORIGINS` | Public origin, e.g. `https://iam.example.invalid`. |
| `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET` | The sign-in client ([authentication.md](authentication.md)). |
| `OIDC_ISSUER` **or** `KEYCLOAK_URL` + `KEYCLOAK_REALM` | The sign-in provider. |

## Optional

| Variable | Default | Meaning |
| --- | --- | --- |
| `OIDC_PROVIDER_NAME` | `Keycloak` or `single sign-on` | Text of the sign-in button. |
| `OIDC_SCOPES` | `openid email profile` | Add `groups` if the provider needs a scope for the group claim. |
| `OIDC_SIGN_ALGO` | `RS256` | ID token signature algorithm. |
| `OIDC_GROUPS_CLAIM` | `groups` | Claim with group names or paths. |
| `OIDC_ADMIN_GROUPS`, `OIDC_APPROVER_GROUPS`, `OIDC_VIEWER_GROUPS` | empty | Comma-separated groups that grant a role at sign-in ([authentication.md](authentication.md)). |
| `BOOTSTRAP_ADMIN_EMAILS` | empty | Verified addresses that may claim the first admin while no active admin exists. |
| `OIDC_AUTHORIZATION_ENDPOINT`, `OIDC_TOKEN_ENDPOINT`, `OIDC_USERINFO_ENDPOINT`, `OIDC_JWKS_ENDPOINT` | from discovery | Only for providers without a discovery document; they win over its values. With all four set, sign-in also works when the provider has none (sign-out then stays local). |
| `SESSION_HOURS` | `12` | How long a sign-in lasts. Also the longest time a role removed at the sign-in provider still works ([authentication.md](authentication.md)). |
| `TRUST_PROXY_HEADERS` | `0` (`1` in `compose.yaml`) | Trust `X-Forwarded-Proto` from the reverse proxy. Enable only behind a proxy that sets it. |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` (`*` in `compose.yaml`) | Proxies gunicorn trusts for forwarded headers. |
| `SECURE_SSL_REDIRECT` | `1` | Redirect HTTP to HTTPS (health checks under `/-/` are exempt). |
| `SECURE_HSTS_INCLUDE_SUBDOMAINS` | `1` | Send HSTS for subdomains too. |
| `HEALTHCHECK_TOKEN` | empty | Shows details on `/-/readyz` for requests with `X-Health-Token`. |
| `UPDATER_URL` | `http://updater:8080` | The `updater` service that the admins' update button and Admin › Settings › Updates talk to ([updates.md](updates.md)). Under `compose.yaml`, an empty value falls back to this default; empty switches the update button off only where the app reads its environment directly, such as `manage.py` with `.env` in development. The `updater` service's own variables (`UPDATE_*`) are in updates.md. |
| `READ_ONLY` | `0` | `1`: read-only mode. The app reads connected apps but changes nothing in them and sends no email; jobs only check what they would change ([architecture.md](architecture.md#read-only-mode)). Set it for web and worker alike (`compose.yaml` does). Any value other than `1`/`0` (or `true`/`false`, `yes`/`no`, `on`/`off`) stops the app from starting. |
| `TZ` | `UTC` | Time zone for dates, start and end days, and the schedule. |
| `WEB_WORKERS`, `WEB_THREADS` | `2`, `4` | gunicorn processes and threads. |
| `WORKER_POLL_SECONDS` | `5` | How often an idle worker looks for jobs. |
| `WORKER_HEARTBEAT_MAX_AGE_SECONDS` | `300` | After this, the dashboard and `/-/readyz` report the worker as stale. |
| `WORKER_STALL_SECONDS` | `600` | A worker without any progress for this long is stuck: it logs where it was and ends, and the container restarts it ([architecture.md](architecture.md#schedule)). |
| `JOB_LEASE_SECONDS` | `300` | A running job whose worker stopped runs again after this. |
| `PROVIDER_TIMEOUT_SECONDS` | `20` | Timeout of each call to a connected app. |
| `SENTRY_DSN`, `SENTRY_ENVIRONMENT`, `SENTRY_RELEASE` | empty | Error reporting without personal data. The release defaults to `iam@<version>`. |
| `SENTRY_TRACES_SAMPLE_RATE` | `0` | Performance tracing share. |

## Development only

| Variable | Meaning |
| --- | --- |
| `DJANGO_DEBUG=1` with `DEPLOYMENT_ENV=development` | Development mode. Without `ENCRYPTION_KEY`, a key is derived from the development secret key. |
| `DEV_LOGIN=1` | Sign in as any app user without a provider (`/login/dev/`). Ignored unless `DJANGO_DEBUG=1`. |

## Rotating the encryption key

1. Create a new key: `python manage.py generate_key`.
2. Set `ENCRYPTION_KEY=<new>,<old>` and redeploy.
3. Run `python manage.py rotate_secrets` once.
4. Set `ENCRYPTION_KEY=<new>` and redeploy.
