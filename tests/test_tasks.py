import logging

from procrastinate.testing import InMemoryConnector

from app.tasks import procrastinate_app
from app.tasks.system import ping


async def test_worker_executes_ping_task(job_queue: InMemoryConnector, caplog):
    caplog.set_level(logging.INFO, logger="app.tasks.system")

    async with procrastinate_app.open_async():
        await ping.defer_async(message="hello")
        assert [job["status"] for job in job_queue.jobs.values()] == ["todo"]

        # wait=False: process what is queued, then return.
        await procrastinate_app.run_worker_async(wait=False)

    assert [job["status"] for job in job_queue.jobs.values()] == ["succeeded"]
    assert "ping task executed message=hello" in caplog.text
