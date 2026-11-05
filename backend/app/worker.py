"""Background worker: claims jobs from the queue and dispatches them to registered handlers.

Handlers are registered by later phases (scans, workflow runs, CI polling).
Run: python -m app.worker
"""

import asyncio
import logging
import os
import signal
import socket
import traceback
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app import queue
from app.config import get_settings
from app.db import SessionLocal
from app.models import ActivityEvent, Job, Worker

log = logging.getLogger("debugops.worker")

Handler = Callable[[AsyncSession, Job], Awaitable[None]]
HANDLERS: dict[str, Handler] = {}

POLL_SECONDS = 2.0
HEARTBEAT_SECONDS = 15.0
SCHEDULE_SECONDS = 60.0
PR_REFRESH_SECONDS = 900.0


def handler(kind: str):
    def register(fn: Handler) -> Handler:
        HANDLERS[kind] = fn
        return fn
    return register


async def heartbeat(worker_id: str) -> None:
    async with SessionLocal() as db:
        now = datetime.now(UTC)
        await db.execute(
            insert(Worker).values(id=worker_id, started_at=now, last_seen_at=now)
            .on_conflict_do_update(index_elements=[Worker.id], set_={"last_seen_at": now})
        )
        await db.execute(delete(Worker).where(Worker.last_seen_at < now - timedelta(minutes=10)))  # restarted/retired workers
        await db.commit()


async def _keep_lock(job_id: int, worker_id: str) -> None:
    while True:
        await asyncio.sleep(queue.LOCK_REFRESH_SECONDS)
        try:
            async with SessionLocal() as db:
                if not await queue.refresh_lock(db, job_id, worker_id):
                    log.error("lost lock on job %s", job_id)
                    return
        except Exception:
            log.exception("lock refresh for job %s failed", job_id)


async def run_one(worker_id: str) -> bool:
    """Claim and process a single job. Returns False if the queue was empty."""
    async with SessionLocal() as db:
        job = await queue.claim(db, worker_id)
        if job is None:
            return False
        fn = HANDLERS.get(job.kind)
        keepalive = asyncio.create_task(_keep_lock(job.id, worker_id))
        try:
            if fn is None:
                raise LookupError(f"No handler registered for job kind '{job.kind}'")
            try:
                await fn(db, job)
            finally:
                keepalive.cancel()
            await queue.complete(db, job)
        except Exception as e:  # noqa: BLE001 — every failure must be recorded, never swallowed
            await db.rollback()
            log.exception("job %s (%s) failed", job.id, job.kind)
            job = await db.get(Job, job.id)
            db.add(ActivityEvent(level="error", type="error", source=worker_id,
                                 message=f"Job {job.id} ({job.kind}) failed: {e}",
                                 data={"job_id": job.id, "traceback": traceback.format_exc()[-4000:]}))
            if fn is None:
                job.attempts = job.max_attempts  # retrying will not help
            await queue.fail(db, job, f"{type(e).__name__}: {e}")
        return True


async def main() -> None:
    from app import observability
    observability.setup("worker")
    worker_id = f"{socket.gethostname()}-{os.getpid()}"
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    import app.detection.jobs as jobs  # registers job handlers
    import app.workflow.jobs as wf_jobs  # registers workflow handlers
    from app.sandbox.runner import cleanup_orphans

    try:
        removed = await asyncio.to_thread(cleanup_orphans, get_settings())
        if removed:
            log.info("removed %d orphaned sandbox container(s)", removed)
    except Exception:
        log.exception("sandbox orphan cleanup failed (is Docker reachable via DOCKER_HOST?)")

    log.info("worker %s started; handlers: %s", worker_id, sorted(HANDLERS) or "none")
    last_beat = last_schedule = last_pr_refresh = 0.0
    while not stop.is_set():
        if loop.time() - last_beat >= HEARTBEAT_SECONDS:
            if last_beat and loop.time() - last_beat > 3 * HEARTBEAT_SECONDS:
                log.warning("worker loop was blocked for %.0fs (heartbeat late); investigate if this repeats",
                            loop.time() - last_beat)
            try:
                await heartbeat(worker_id)
                last_beat = loop.time()
            except Exception:
                log.exception("heartbeat failed")
        if loop.time() - last_schedule >= SCHEDULE_SECONDS:
            last_schedule = loop.time()
            try:
                async with SessionLocal() as db:
                    await jobs.enqueue_scheduled_scans(db)
                if loop.time() - last_pr_refresh >= PR_REFRESH_SECONDS:
                    last_pr_refresh = loop.time()
                    async with SessionLocal() as db:
                        await wf_jobs.refresh_open_prs(db)
            except Exception:
                log.exception("scheduler failed")
        try:
            busy = await run_one(worker_id)
        except Exception:
            log.exception("queue error")
            busy = False
        if not busy:
            try:
                await asyncio.wait_for(stop.wait(), timeout=POLL_SECONDS)
            except TimeoutError:
                pass
    log.info("worker %s stopped", worker_id)


if __name__ == "__main__":
    # Run via the importable module, not __main__: handlers register on `app.worker.HANDLERS`,
    # and `python -m` would otherwise create a second, empty copy of this module.
    from app.worker import main as _main

    asyncio.run(_main())
