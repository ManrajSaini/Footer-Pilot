# syntax=docker/dockerfile:1
# One image for both processes: the default command serves the web app,
# and `python -m app.cli worker` runs the background worker.

FROM python:3.13-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.11.21 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app

COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev

COPY scripts/ scripts/
COPY app/ app/
RUN python scripts/tailwind.py build


FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"
RUN useradd --system --create-home --uid 1000 app
WORKDIR /app

COPY --from=build /app/.venv .venv
COPY --from=build /app/app app
COPY alembic.ini ./
COPY alembic/ alembic/

USER app
EXPOSE 8000
CMD ["python", "-m", "app.cli", "web", "--host", "0.0.0.0"]
