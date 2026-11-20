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
npm ci                                   # Node.js 26
npm run db:migrate
npm run dev                              # http://localhost:5173
```

`npm run dev` starts the server (API, sign-in, worker; port 8000, reloads on
changes) and Vite with the web app on port 5173, which passes `/api`,
`/oidc` and `/-/` on to the server. The server reads `.env`; `APP_URL` is
Vite's address, which the development realm allows as a sign-in redirect.

The repository is an npm workspace:

| Folder | What |
| --- | --- |
| `apps/server` | Hono server: the API (`src/modules`), sign-in (`src/auth`, `src/http/sign-in.ts`), the engine (`src/engine`), the adapters (`src/adapters`), the worker (`src/worker`), the database (`src/db`: Drizzle schema, migrations in `drizzle/`, the takeover of a Django database in `django.ts`), operator commands (`src/cli.ts`) |
| `apps/web` | React web app: the app frame and pages from `@meldgrove/ui`, TanStack Router and Query; one folder per area in `src/modules` |
| `packages/shared` | API types and request schemas (`src/api/iam.ts`), labels of the choices (`src/labels.ts`) |

The theme, the components, the app frame, the update button and the eye
button of secret fields come from the npm package `@meldgrove/ui`, which the
Meldgrove apps share ([meldgrove/ui](https://github.com/meldgrove/ui)).

To try the app with the development Keycloak as a connection: Admin ›
Connections › Add › Keycloak with URL `http://localhost:8098`, realm `iam`,
client `iam-provisioner` and its secret. For email: Email (SMTP), host
`localhost`, port `1027`, security none.

`npm run cli --workspace @iam/server -- seed-demo` adds demo people, profiles,
teams and a manual app. Without Keycloak, set `DEV_LOGIN=1` in `.env` and sign
in at `/login/dev` as any app user (development mode only). Other commands:
`generate-key`, `create-admin --email <address>`, `rotate-secrets`.

A schema change: edit `apps/server/src/db/schema.ts`, then `npm run
db:generate` writes the migration. Keep the names of the Django version for
existing tables, and give a new column of an existing table a database default
(the Django version must still run after a rollback).

## Checks

CI (`.github/workflows/ci.yml`) runs the same:

```sh
npm run lint && npm run format:check && npm run typecheck
npm test                                                   # unit tests
TEST_DATABASE_URL=postgres://iam:iam@localhost:55438/iam_test npm run test:integration
npm run db:generate                                        # must leave apps/server/drizzle unchanged
node scripts/check-public-repository.mjs
```

Integration tests recreate the database of `TEST_DATABASE_URL`. Adapters are
tested against fake HTTP answers (`src/test/mock-fetch.ts`); the engine
against an in-memory adapter (`src/test/fake-adapter.ts`). One test takes over
a database of the Django version (`src/db/fixtures/django-0.2.sql`) and checks
that it ends with the schema of a new one. No test reaches an updater.

The update button needs an updater at `UPDATER_URL` (`.env.example` leaves it
empty, which hides the button). Any server that answers the updater's API
([docs/updates.md](updates.md)) works, such as a stand-in that returns a fixed
status.

## End-to-end check

`tests/browser/e2e.mjs` drives a browser through the real flow against the
local stack: sign-in through Keycloak, Keycloak and SMTP connections set up in
the admin section, a profile, a team and a person, onboarding (it checks the
Keycloak user, its groups and the setup email in Mailpit), offboarding (user
disabled, managed groups gone), account comparison and sign-out. It needs
the app on port 8000 and a running worker.

```sh
cd tests/browser && npm ci && npx playwright install chromium && cd ../..
node tests/browser/e2e.mjs          # screenshots in test-results/
```

CI runs it against the production image.

## Conventions

- Adapters never touch the database and never put secrets or provider
  responses in error messages.
- Every change by a person or the worker writes an audit event
  (`record` in `apps/server/src/engine/audit.ts`), never with secret values.
- Use only `example.invalid` in examples; `scripts/check-public-repository.mjs`
  rejects other hosts and credential-shaped values.
