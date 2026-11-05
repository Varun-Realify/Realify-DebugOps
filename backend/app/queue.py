"""Postgres-backed job queue (SELECT ... FOR UPDATE SKIP LOCKED).

ponytail: polling + Postgres row locks; good to ~100 jobs/s. Swap for Redis Streams behind
these same functions if throughput ever needs it.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Job, JobStatus

STALE_LOCK = timedelta(minutes=5)  # live workers refresh locked_at every LOCK_REFRESH; a dead worker's job is re-queued after this
LOCK_REFRESH_SECONDS = 30


async def enqueue(db: AsyncSession, kind: str, payload: dict | None = None, *, priority: int = 0,
                  max_attempts: int = 3, delay: timedelta | None = None) -> Job:
    job = Job(kind=kind, payload=payload or {}, priority=priority, max_attempts=max_attempts,
              run_at=datetime.now(UTC) + (delay or timedelta()))
    db.add(job)
    await db.flush()
    return job


async def claim(db: AsyncSession, worker_id: str) -> Job | None:
    """Atomically claim the next due job. Commits."""
    now = datetime.now(UTC)
    # Recover jobs orphaned by a crashed worker.
    await db.execute(
        update(Job)
        .where(Job.status == JobStatus.running, Job.locked_at < now - STALE_LOCK)
        .values(status=JobStatus.queued, locked_by=None, locked_at=None)
    )
    job = (
        await db.execute(
            select(Job)
            .where(Job.status == JobStatus.queued, Job.run_at <= now)
            .order_by(Job.priority.desc(), Job.run_at, Job.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
    ).scalar_one_or_none()
    if job:
        job.status = JobStatus.running
        job.locked_by = worker_id
        job.locked_at = now
        job.attempts += 1
    await db.commit()
    return job


async def refresh_lock(db: AsyncSession, job_id: int, worker_id: str) -> bool:
    """Keep a long-running job's lock fresh so it is never mistaken for an orphan. Returns False if we lost it."""
    res = await db.execute(update(Job).where(Job.id == job_id, Job.locked_by == worker_id, Job.status == JobStatus.running)
                           .values(locked_at=datetime.now(UTC)))
    await db.commit()
    return res.rowcount == 1


async def complete(db: AsyncSession, job: Job) -> None:
    job.status = JobStatus.succeeded
    job.locked_by = job.locked_at = None
    job.last_error = None
    await db.commit()


async def fail(db: AsyncSession, job: Job, error: str) -> None:
    """Retry with exponential backoff until max_attempts, then mark failed."""
    job.last_error = error[:5000]
    job.locked_by = job.locked_at = None
    if job.attempts < job.max_attempts:
        job.status = JobStatus.queued
        job.run_at = datetime.now(UTC) + timedelta(seconds=10 * 2 ** (job.attempts - 1))
    else:
        job.status = JobStatus.failed
    await db.commit()
