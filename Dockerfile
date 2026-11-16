FROM ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 AS uv
FROM python:3.13-slim-bookworm@sha256:a5a7017efc5969e00ae2b91c8eb0631049a569652aa4ffc4501769049ae45c72 AS base
COPY --from=uv /uv /usr/local/bin/uv
RUN groupadd --gid 10001 iam && useradd --uid 10001 --gid iam --create-home iam
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./

FROM base AS test
ENV DJANGO_DEBUG=1 DEPLOYMENT_ENV=development
RUN uv sync --locked
COPY . .
RUN chown -R iam:iam /app
USER iam
ENV PATH="/app/.venv/bin:$PATH"
CMD ["sh", "-c", "ruff check . && ruff format --check . && python manage.py makemigrations --check --dry-run && pytest"]

FROM base AS runtime
RUN uv sync --locked --no-dev
COPY . .
# Static files need only development defaults, whatever build arguments Coolify passes.
RUN env -i PATH="$PATH" PYTHONDONTWRITEBYTECODE=1 DJANGO_DEBUG=1 DEPLOYMENT_ENV=development \
    /app/.venv/bin/python manage.py collectstatic --noinput \
    && mkdir -p var && chown -R iam:iam /app/var
# The release workflow sets the version; images built from source have none.
ARG APP_VERSION=""
ENV APP_VERSION=${APP_VERSION}
USER iam
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000
CMD ["sh", "/app/scripts/start-web.sh"]
