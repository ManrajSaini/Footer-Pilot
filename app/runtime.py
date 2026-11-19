"""Event loop selection.

psycopg's async mode cannot run on the Proactor loop, which is asyncio's default on
Windows. Every entry point (web, worker, migrate, tests) therefore creates its loop
through this module so that local development works on Windows as well as Linux.
"""

import asyncio
import sys
from collections.abc import Coroutine
from typing import Any


def new_event_loop() -> asyncio.AbstractEventLoop:
    if sys.platform == "win32":
        return asyncio.SelectorEventLoop()
    return asyncio.new_event_loop()


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro, loop_factory=new_event_loop)
