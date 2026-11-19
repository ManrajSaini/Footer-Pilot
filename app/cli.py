"""Command-line entry points: `python -m app.cli <command>`.

web      Serve the web app (add --reload in development)
worker   Run the background job worker
migrate  Apply database migrations and the job queue schema
ping     Queue a test job, to confirm the worker is processing
"""

import argparse
import contextlib
import logging
import os
import sys
from pathlib import Path

from app.config import get_settings
from app.log import configure_logging
from app.runtime import run

ROOT = Path(__file__).resolve().parent.parent
logger = logging.getLogger("app.cli")


def web(args: argparse.Namespace) -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "app.main:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        reload=args.reload,
        reload_dirs=[str(ROOT / "app")] if args.reload else None,
        # psycopg needs the selector loop on Windows; elsewhere let uvicorn choose.
        loop="app.runtime:new_event_loop" if sys.platform == "win32" else "auto",
        # In production the app is only reachable through the host's proxy.
        forwarded_allow_ips="*" if settings.is_production else None,
    )


def worker(args: argparse.Namespace) -> None:
    from app.tasks import procrastinate_app
    from app.tasks.context import build_context, set_context

    async def main() -> None:
        context = build_context(get_settings())
        set_context(context)
        try:
            async with procrastinate_app.open_async():
                await procrastinate_app.run_worker_async(concurrency=args.concurrency)
        finally:
            await context.db.dispose()

    run(main())


def migrate(args: argparse.Namespace) -> None:
    import psycopg
    from alembic.config import Config

    from alembic import command
    from app.tasks import procrastinate_app

    command.upgrade(Config(str(ROOT / "alembic.ini")), "head")

    # Procrastinate ships its own schema. Install it once; later Procrastinate
    # upgrades come with their own migration scripts (see README).
    async def ensure_queue_schema() -> None:
        dsn = get_settings().database_dsn
        async with await psycopg.AsyncConnection.connect(dsn) as connection:
            cursor = await connection.execute("SELECT to_regclass('procrastinate_jobs')")
            row = await cursor.fetchone()
        if row and row[0]:
            logger.info("job queue schema already present")
            return
        async with procrastinate_app.open_async():
            await procrastinate_app.schema_manager.apply_schema_async()
        logger.info("job queue schema installed")

    run(ensure_queue_schema())


def ping(args: argparse.Namespace) -> None:
    from app.tasks import procrastinate_app
    from app.tasks.system import ping as ping_task

    async def main() -> None:
        async with procrastinate_app.open_async():
            job_id = await ping_task.defer_async(message=args.message)
        logger.info("ping job queued job_id=%s", job_id)

    run(main())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description="FooterPilot commands")
    commands = parser.add_subparsers(dest="command", required=True)

    web_parser = commands.add_parser("web", help="serve the web app")
    web_parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    web_parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    web_parser.add_argument("--reload", action="store_true", help="restart on code changes")
    web_parser.set_defaults(func=web)

    worker_parser = commands.add_parser("worker", help="run the background job worker")
    worker_parser.add_argument("--concurrency", type=int, default=10)
    worker_parser.set_defaults(func=worker)

    migrate_parser = commands.add_parser("migrate", help="apply database migrations")
    migrate_parser.set_defaults(func=migrate)

    ping_parser = commands.add_parser("ping", help="queue a test job")
    ping_parser.add_argument("--message", default="pong")
    ping_parser.set_defaults(func=ping)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    configure_logging(get_settings().log_level)
    with contextlib.suppress(KeyboardInterrupt):
        args.func(args)


if __name__ == "__main__":
    main()
