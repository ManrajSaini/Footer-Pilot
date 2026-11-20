"""Background jobs (Procrastinate, stored in Postgres).

Add each new task module to `import_paths` so the worker registers its tasks.
"""

from procrastinate import App, PsycopgConnector

from app.config import get_settings

procrastinate_app = App(
    connector=PsycopgConnector(conninfo=get_settings().database_dsn),
    import_paths=[
        "app.tasks.system",
        "app.tasks.sync",
        "app.tasks.deploy",
        "app.tasks.periodic",
    ],
)
