# Development

## Local stack

`compose.dev.yaml` runs the services the app talks to:

| Service | Address | Login |
| --- | --- | --- |
| PostgreSQL 18 | `localhost:55438`, database `iam` | `iam` / `iam` |
| Keycloak 26 | http://localhost:8098 | console `admin` / `admin`; realm `iam`, user `admin` / `admin` (member of `/iam/admins`) |
| Mailpit | http://localhost:8027, SMTP `localhost:1027` | none |

The realm (`dev/keycloak/iam-realm.json`) has the sign-in client `iam`
(secret `iam-dev-client-secret`, with a group claim), the provisioning client
`iam-provisioner` (secret `iam-dev-provisioner-secret`, service account with
the realm-management roles and the `kc-admin-api` scope the Keycloak connection
needs), groups under
`/apps`, `/teams` and `/iam`, and sends its emails to Mailpit.

```sh
docker compose -f compose.dev.yaml up -d
cp .env.example .env
uv sync
npm ci                                   # Node.js 26; the files from @meldgrove/ui
uv run python manage.py migrate
uv run python manage.py runserver        # http://localhost:8000
uv run python manage.py run_worker       # second terminal
```

The update button, the Updates settings and the colors come from the npm
package `@meldgrove/ui`, which the Meldgrove apps share. `npm ci` copies two
of its files to `iam/static/iam/vendor/` (ignored by git):
`meldgrove-updates.js` (the custom elements `<meldgrove-update-button>` and
`<meldgrove-updates-settings>`) and `tokens.css` (the colors, light and dark).
Run `npm ci` again after an update of the package; the Docker image copies
them in its `ui` stage. `app.css` uses the tokens' names (`--background`,
`--foreground`, `--muted-foreground`, `--primary`, …) and sets IAM's accent
(`--mg-accent`). Dark mode is the class `dark` on `<html>`, which
`static/iam/theme.js` sets from the device's setting.

`manage.py` reads `.env`. To try the app with the development Keycloak as a
connection: Admin › Connections › Add › Keycloak with URL
`http://localhost:8098`, realm `iam`, client `iam-provisioner` and its secret.
For email: Email (SMTP), host `localhost`, port `1027`, security none.

`uv run python manage.py seed_demo` adds demo people, profiles, teams and a
manual app. Without Keycloak, set `DEV_LOGIN=1` in `.env` and sign in at
`/login/dev/` as any app user (development mode only).

## Checks

CI (`.github/workflows/ci.yml`) runs the same:

```sh
uv run ruff check . && uv run ruff format --check .
uv run python manage.py makemigrations --check --dry-run
uv run pytest                                               # SQLite
TEST_DATABASE_URL=postgresql://iam:iam@localhost:55438/iam uv run pytest
python3 scripts/check_public_repository.py
```

Adapters are tested against `httpx.MockTransport` fakes (`mock_transport`
fixture); the engine against an in-memory adapter (`tests/fakes.py`). No test
reaches an updater: the `updater` fixture answers for it.

The update button needs an updater at `UPDATER_URL` (`.env.example` leaves it
empty, which hides the button). Any server that answers the updater's API
([docs/updates.md](updates.md)) works, such as a stand-in that returns a fixed
status.

## End-to-end check

`tests/browser/e2e.cjs` drives a browser through the real flow against the
local stack: sign-in through Keycloak, Keycloak and SMTP connections set up in
the admin section, a profile, a team and a person, onboarding (it checks the
Keycloak user, its groups and the setup email in Mailpit), offboarding (user
disabled, managed groups gone), account comparison and sign-out. It needs
the app on port 8000 and a running worker.

```sh
cd tests/browser && npm ci && npx playwright install chromium && cd ../..
node tests/browser/e2e.cjs          # screenshots in test-results/
```

CI runs it against the production image.

## Conventions

- Adapters never touch the database and never put secrets or provider
  responses in error messages.
- Every change by a person or the worker writes an audit event
  (`iam.audit.record`), never with secret values.
- Use only `example.invalid` in examples; `scripts/check_public_repository.py`
  rejects other hosts and credential-shaped values.
