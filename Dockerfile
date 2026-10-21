# syntax=docker/dockerfile:1
FROM python:3.13-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY apps ./apps
COPY config ./config
COPY manage.py ./
# collectstatic needs settings but no database or real secrets.
RUN DJANGO_DEBUG=1 .venv/bin/python manage.py collectstatic --noinput

FROM python:3.13-slim
RUN useradd --system --uid 10001 --home-dir /app app \
    && mkdir -p /app/media && chown app /app/media
WORKDIR /app
COPY --from=build --chown=root:root /app /app
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 MEDIA_ROOT=/app/media
USER app
EXPOSE 8000
CMD ["gunicorn", "config.wsgi", "--bind", "0.0.0.0:8000", "--workers", "3", "--timeout", "60", "--access-logfile", "-"]
