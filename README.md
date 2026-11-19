# FooterPilot

Company-wide email signature manager for Google Workspace. Product description: `idea.md`. Build plan: `plan.md`. Deferred ideas: `later.md`.

## Requirements

- Python 3.12+ and [uv](https://docs.astral.sh/uv/)
- Docker (for the local Postgres database)

## First-time setup

```sh
uv sync                              # install dependencies into .venv
cp .env.example .env                 # optional: the defaults work for local development
docker compose up -d                 # start Postgres (creates dev and test databases)
uv run python -m app.cli migrate     # create tables and the job queue schema
uv run python scripts/tailwind.py build   # build app/static/css/app.css
```

## Everyday commands

| Task | Command |
|---|---|
| Run the web app (http://127.0.0.1:8000) | `uv run python -m app.cli web --reload` |
| Run the background worker | `uv run python -m app.cli worker` |
| Queue a test job (the worker logs it) | `uv run python -m app.cli ping` |
| Apply migrations | `uv run python -m app.cli migrate` |
| Create a migration after changing models | `uv run alembic revision --autogenerate -m "describe change"` |
| Rebuild CSS while editing templates | `uv run python scripts/tailwind.py watch` |
| Run tests | `uv run pytest` |
| Lint and format | `uv run ruff check .` and `uv run ruff format .` |

The health check is at `/healthz` (returns 503 when the database is unreachable).

## Notes

- **Tests** use the `footerpilot_test` database from docker-compose and an in-memory job queue. They never call a real external service. Postgres must be running.
- **Database host:** use `127.0.0.1`, not `localhost`. On Windows, `localhost` tries IPv6 first and each connection stalls for seconds against Docker's port mapping.
- **Windows:** always start the app through `python -m app.cli`. It selects an event loop that the Postgres driver supports; plain `uvicorn app.main:create_app` does not.
- **CSS:** `app/static/css/app.css` is generated and not committed. The build script downloads the Tailwind standalone CLI into `.tools/`. If that download is blocked and Node is installed, it installs the same Tailwind version from npm instead.
- **Vendored scripts:** HTMX 2.0.11 and Alpine.js 3.17.5 live in `app/static/vendor/`. To upgrade, replace the files and update the two `<script>` tags in `app/templates/base.html`.
- **Upgrading Procrastinate:** `migrate` installs its schema only once. When bumping the Procrastinate version, apply the SQL migration scripts listed in its release notes.
- **Production settings:** with `ENV=production` the app refuses to start if `GOOGLE_MODE=fake`, `DEV_LOGIN=true`, `STORAGE_BACKEND=local` or `SECRET_KEY` is unset.

## Docker image

One image serves both processes.

```sh
docker build -t footerpilot .
docker run -e DATABASE_URL=... -p 8000:8000 footerpilot                      # web
docker run -e DATABASE_URL=... footerpilot python -m app.cli worker          # worker
docker run -e DATABASE_URL=... footerpilot python -m app.cli migrate         # migrations
```

## Layout

```
app/
  main.py        app factory
  cli.py         web / worker / migrate / ping commands
  config.py      settings from environment variables
  db.py          engine and session dependency
  models/        SQLAlchemy models
  web/           routers, CSRF, error pages, template setup
  tasks/         background jobs (Procrastinate)
  templates/     Jinja pages
  static/        CSS, vendored JS
alembic/         database migrations
scripts/         build helpers
tests/
```
