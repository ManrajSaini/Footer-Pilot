"""What background tasks need to do their work: database, Google and storage.

The web app keeps these on `app.state`. The worker has no request, so it keeps one
set here. `cli.worker` builds it at start-up; tests point it at their own fakes.
"""

from dataclasses import dataclass

from app.config import Settings
from app.db import Database
from app.google import build_gateways
from app.google.base import GoogleGateways
from app.storage import Storage, build_storage


@dataclass
class WorkerContext:
    db: Database
    google: GoogleGateways
    storage: Storage


_context: WorkerContext | None = None


def build_context(settings: Settings) -> WorkerContext:
    return WorkerContext(
        db=Database(settings.sqlalchemy_url),
        google=build_gateways(settings),
        storage=build_storage(settings),
    )


def set_context(context: WorkerContext | None) -> None:
    global _context
    _context = context


def get_context() -> WorkerContext:
    if _context is None:
        raise RuntimeError("worker context is not set; tasks only run inside the worker")
    return _context
