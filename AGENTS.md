# AGENTS.md

Meldgrove IAM: a TypeScript app (Node.js 26, PostgreSQL) that manages people,
access profiles, teams and their accounts in connected apps. npm workspaces:
`apps/server` (Hono, Drizzle), `apps/web` (React, Vite, TanStack Router and
Query, `@meldgrove/ui`) and `packages/shared` (API types and request schemas,
labels). One image, three processes: `migrate`, `web` and `worker`
(`APP_ROLE`, `docker-entrypoint.sh`). Versions 0.1 and 0.2 were a Django app.

- Domain language: `CONTEXT.md`. How it works: `docs/architecture.md`.
- Adapters live in `apps/server/src/adapters/`, one module per connected app,
  registered in `registry.ts`. They get their configuration as an object, never
  touch the database, and never put secrets or provider responses into error
  messages. What a job does at an account is decided from reads in the
  adapter's plan (`planEnsure`, `planDisable`, `planGroups`); the job follows it
  and read-only mode rehearses it, so put a new decision or refusal there, not
  in `ensureAccount` or `setActive` alone. Connection settings are configured
  in the admin UI, not in environment variables.
- The engine (`apps/server/src/engine/`): `policy.ts` (desired access),
  `lifecycle.ts` (starting and cancelling workflows), `jobs.ts` (running jobs;
  the only place that changes job, task and workflow status: `runJob` is
  `attempt`, which talks to the connection and writes nothing, then `finish`,
  the only code that settles a claimed job), `accounts.ts` (the only writer of
  Account rows), `sync.ts` (reads), `schedule.ts`. Transactions go through
  `db/tx.ts` (`atomic`, `q()`, `onCommit`); keep the lock order person, then
  job or workflow.
- The database keeps the table, column, constraint and index names of the
  Django version (`db/schema.ts`); `db/django.ts` takes over a Django database
  in place. Add columns to existing tables with a database default, so the
  Django version still runs after a rollback. Migrations: `npm run db:generate`.
- Every change by a person or the worker writes an audit event without secret
  values.
- The web app shows its pages in the app frame of `@meldgrove/ui`; the update
  button and the "Updates" card come from the package too. Secrets use its
  `SecretField`/`SecretInput`. Admin routes for updates: `/api/v1/admin/updates`.
- The repository is public and licensed under the Apache License 2.0
  (`LICENSE`). Use only `example.invalid` in examples;
  `scripts/check-public-repository.mjs` must pass.

## Checks

```sh
npm run lint && npm run format:check && npm run typecheck
npm test                                                   # unit tests
TEST_DATABASE_URL=postgres://iam:iam@localhost:55438/iam_test npm run test:integration
npm run db:generate                                        # must leave apps/server/drizzle unchanged
node scripts/check-public-repository.mjs
```

The end-to-end check (`tests/browser/e2e.mjs`) needs the development stack
(`compose.dev.yaml`), the app on port 8000 and a worker; see
`docs/development.md`. CI runs everything above on every pull request.
