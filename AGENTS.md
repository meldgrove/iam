# AGENTS.md

Meldgrove IAM: a Django 6.1 app (Python 3.13, PostgreSQL) that manages people,
access profiles, teams and their accounts in connected apps. One image, three
processes: `migrate`, `web` (gunicorn) and `worker` (`manage.py run_worker`).

- Domain language: `CONTEXT.md`. How it works: `docs/architecture.md`.
- Adapters live in `iam/adapters/`, one module per connected app, registered
  in `iam/adapters/__init__.py`. They get their configuration as a dict, never
  touch the database, and never put secrets or provider responses into error
  messages. Connection settings are configured in the admin UI, not in
  environment variables.
- The engine: `iam/policy.py` (desired access), `iam/lifecycle.py`
  (starting and cancelling workflows), `iam/jobs.py` (running jobs; the only
  place that changes job, task and workflow status), `iam/accounts.py` (the
  only writer of Account rows), `iam/sync.py` (reads), `iam/schedule.py`.
- Every change by a person or the worker writes an audit event without secret
  values.
- Templates render the pages; `iam/static/iam/app.css` styles them with the
  shared color tokens. The update button and the Updates settings are custom
  elements from the npm package `@meldgrove/ui`: `npm ci` copies them and the
  tokens to `iam/static/iam/vendor/` (`docs/development.md`). The app keeps
  only the routes under `/admin/updates/` (`iam/views/updates.py`).
- The repository is public and has no license yet. Do not add a license file.
  Use only `example.invalid` in examples; `scripts/check_public_repository.py`
  must pass.

## Checks

```sh
uv run ruff check . && uv run ruff format --check .
uv run python manage.py makemigrations --check --dry-run
uv run pytest
TEST_DATABASE_URL=postgresql://iam:iam@localhost:55438/iam uv run pytest
python3 scripts/check_public_repository.py
```

The end-to-end check (`tests/browser/e2e.cjs`) needs the development stack
(`compose.dev.yaml`), the app on port 8000 and a worker; see
`docs/development.md`. CI runs everything above on every pull request.
