import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from app import queue, worker
from app.db import SessionLocal
from app.models import ActivityEvent, Job, JobStatus


async def _enqueue(kind="test.ok", **kw) -> int:
    async with SessionLocal() as db:
        j = await queue.enqueue(db, kind, {"x": 1}, **kw)
        await db.commit()
        return j.id


async def test_concurrent_claims_never_share_a_job():
    ids = {await _enqueue() for _ in range(10)}

    async def claimer(n):
        got = []
        async with SessionLocal() as db:
            while job := await queue.claim(db, f"w{n}"):
                got.append(job.id)
        return got

    results = await asyncio.gather(*(claimer(n) for n in range(4)))
    claimed = [j for r in results for j in r]
    assert sorted(claimed) == sorted(ids)  # every job claimed exactly once


async def test_priority_and_delay_respected():
    low = await _enqueue(priority=0)
    high = await _enqueue(priority=10)
    await _enqueue(priority=99, delay=timedelta(hours=1))  # not due yet
    async with SessionLocal() as db:
        assert (await queue.claim(db, "w")).id == high
        assert (await queue.claim(db, "w")).id == low
        assert await queue.claim(db, "w") is None


async def test_retry_with_backoff_then_failed():
    jid = await _enqueue(max_attempts=2)
    async with SessionLocal() as db:
        job = await queue.claim(db, "w")
        await queue.fail(db, job, "boom")
        assert job.status == JobStatus.queued and job.run_at > datetime.now(UTC)
        await db.execute(update(Job).where(Job.id == jid).values(run_at=datetime.now(UTC)))
        await db.commit()
        job = await queue.claim(db, "w")
        await queue.fail(db, job, "boom again")
        assert job.status == JobStatus.failed and job.attempts == 2 and job.last_error == "boom again"


async def test_stale_running_job_is_recovered():
    jid = await _enqueue()
    async with SessionLocal() as db:
        await queue.claim(db, "dead-worker")
        await db.execute(update(Job).where(Job.id == jid).values(locked_at=datetime.now(UTC) - timedelta(hours=1)))
        await db.commit()
        job = await queue.claim(db, "live-worker")
        assert job.id == jid and job.locked_by == "live-worker" and job.attempts == 2


async def test_worker_dispatch_success_and_unknown_kind():
    seen = []

    @worker.handler("test.ok")
    async def ok(db, job):
        seen.append(job.payload)

    ok_id = await _enqueue("test.ok")
    unknown_id = await _enqueue("test.nope")
    assert await worker.run_one("w") and await worker.run_one("w")
    assert not await worker.run_one("w")

    async with SessionLocal() as db:
        assert (await db.get(Job, ok_id)).status == JobStatus.succeeded
        bad = await db.get(Job, unknown_id)
        assert bad.status == JobStatus.failed and "No handler" in bad.last_error  # no pointless retries
        assert (await db.execute(select(ActivityEvent.level))).scalars().all() == ["error"]
    assert seen == [{"x": 1}]
    worker.HANDLERS.pop("test.ok")


async def test_heartbeat_upserts_worker_and_prunes_old():
    from app.models import Worker
    await worker.heartbeat("old")
    async with SessionLocal() as db:
        await db.execute(update(Worker).values(last_seen_at=datetime.now(UTC) - timedelta(minutes=11)))
        await db.commit()
    await worker.heartbeat("w1")
    await worker.heartbeat("w1")
    async with SessionLocal() as db:
        assert (await db.execute(select(Worker.id))).scalars().all() == ["w1"]


def test_worker_entrypoint_registers_handlers():
    """`python -m app.worker` must register job handlers (regression: __main__ vs app.worker module split)."""
    import os
    import subprocess
    import sys

    proc = subprocess.Popen([sys.executable, "-m", "app.worker"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            env={**os.environ, "LOG_LEVEL": "INFO"})  # logs go to stdout (centralized logging)
    try:
        for line in proc.stdout:
            if "started; handlers:" in line:
                assert "scan.run" in line and "github.create_issue" in line, line
                break
        else:
            raise AssertionError("worker never reported startup")
    finally:
        proc.terminate()
        proc.wait(timeout=10)


async def test_long_running_job_keeps_its_lock(monkeypatch):
    """A job running longer than STALE_LOCK must not be handed to a second worker."""
    monkeypatch.setattr(queue, "LOCK_REFRESH_SECONDS", 0.05)
    monkeypatch.setattr(queue, "STALE_LOCK", timedelta(milliseconds=300))
    started, release = asyncio.Event(), asyncio.Event()

    @worker.handler("test.slow")
    async def slow(db, job):
        started.set()
        await release.wait()

    jid = await _enqueue("test.slow")
    task = asyncio.create_task(worker.run_one("w-long"))
    await started.wait()
    await asyncio.sleep(0.8)  # well past STALE_LOCK
    async with SessionLocal() as db:
        assert await queue.claim(db, "w-thief") is None  # still locked by the live worker
    release.set()
    await task
    async with SessionLocal() as db:
        assert (await db.get(Job, jid)).status == JobStatus.succeeded
    worker.HANDLERS.pop("test.slow")
