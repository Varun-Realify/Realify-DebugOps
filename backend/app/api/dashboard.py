from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth
from app.db import get_db
from app.models import ActivityEvent, AgentStep, Bug, WorkflowRun, Job, JobStatus, Repository, Role, ScanRun, TestRun, User, Worker
from app.schemas import ActivityOut, DashboardSummary, WorkerOut

router = APIRouter(tags=["dashboard"])
Db = Annotated[AsyncSession, Depends(get_db)]
WORKER_ONLINE_WINDOW = timedelta(seconds=60)


@router.get("/dashboard/summary", response_model=DashboardSummary)
async def summary(_: auth.CurrentPrincipal, db: Db):
    jobs = dict((await db.execute(select(Job.status, func.count()).group_by(Job.status))).all())
    roles = dict((await db.execute(select(User.role, func.count()).where(User.is_active.is_(True)).group_by(User.role))).all())
    cutoff = datetime.now(UTC) - WORKER_ONLINE_WINDOW
    workers = (await db.execute(select(Worker).order_by(Worker.last_seen_at.desc()).limit(20))).scalars().all()
    events = (await db.execute(select(ActivityEvent).order_by(ActivityEvent.id.desc()).limit(20))).scalars().all()
    open_q = Bug.status.not_in(["resolved", "wont_fix", "duplicate", "closed"])
    by_sev = dict((await db.execute(select(Bug.severity, func.count()).where(open_q).group_by(Bug.severity))).all())
    by_src = dict((await db.execute(select(Bug.source, func.count()).where(open_q).group_by(Bug.source))).all())
    latest = select(func.max(ScanRun.id)).where(ScanRun.status.in_(["succeeded", "partial"])).group_by(ScanRun.repository_id)
    t = (await db.execute(select(func.coalesce(func.sum(TestRun.passed), 0), func.coalesce(func.sum(TestRun.failed), 0),
                                 func.coalesce(func.sum(TestRun.errors), 0), func.coalesce(func.sum(TestRun.skipped), 0))
                          .where(TestRun.scan_run_id.in_(latest)))).one()
    scans = (await db.execute(select(ScanRun, Repository.full_name).join(Repository, Repository.id == ScanRun.repository_id)
                              .order_by(ScanRun.id.desc()).limit(5))).all()
    day = datetime.now(UTC) - timedelta(hours=24)
    steps = dict((await db.execute(select(AgentStep.status, func.count()).where(
        (AgentStep.status.in_(["running", "waiting"])) | (AgentStep.started_at >= day)).group_by(AgentStep.status))).all())
    queued_runs = await db.scalar(select(func.count()).where(WorkflowRun.status == "queued"))
    active = (await db.execute(select(WorkflowRun, Bug.title).join(Bug, Bug.id == WorkflowRun.bug_id)
                               .where(WorkflowRun.status.in_(["queued", "running", "waiting_approval", "awaiting_merge"]))
                               .order_by(WorkflowRun.id.desc()).limit(10))).all()
    by_status = dict((await db.execute(select(WorkflowRun.status, func.count()).group_by(WorkflowRun.status))).all())
    finished_ok = by_status.get("succeeded", 0)
    finished_bad = sum(by_status.get(k, 0) for k in ("failed", "needs_human", "rejected", "closed"))
    tasks = {"total": sum(by_status.values()), "active": sum(by_status.get(k, 0) for k in ("queued", "running")),
             "awaiting_review": by_status.get("waiting_approval", 0) + by_status.get("awaiting_merge", 0),
             "succeeded": finished_ok, "failed": by_status.get("failed", 0), "needs_human": by_status.get("needs_human", 0),
             "success_rate": round(finished_ok / (finished_ok + finished_bad), 3) if finished_ok + finished_bad else None}
    errors_24h = await db.scalar(select(func.count()).where(ActivityEvent.level == "error", ActivityEvent.ts >= day))
    return DashboardSummary(
        tasks=tasks,
        errors_24h=errors_24h or 0,
        agent_status={"running": steps.get("running", 0), "waiting": steps.get("waiting", 0) + (queued_runs or 0),
                      "completed": steps.get("completed", 0), "failed": steps.get("failed", 0)},
        active_runs=[{"id": r.id, "bug_title": t, "status": r.status, "current_node": r.current_node, "attempt": r.attempt}
                     for r, t in active],
        open_bugs_by_severity={k: by_sev.get(k, 0) for k in ("critical", "high", "medium", "low", "info")},
        open_bugs_by_source=by_src,
        latest_tests={"passed": int(t[0]), "failed": int(t[1]), "errors": int(t[2]), "skipped": int(t[3])},
        recent_scans=[{"id": sc.id, "repository": name, "status": sc.status, "trigger": sc.trigger,
                       "created_at": sc.created_at.isoformat(), "findings": sc.summary.get("findings")} for sc, name in scans],
        jobs={s: jobs.get(s, 0) for s in JobStatus},
        users_by_role={r: roles.get(r, 0) for r in Role},
        workers=[WorkerOut(id=w.id, started_at=w.started_at, last_seen_at=w.last_seen_at,
                           online=w.last_seen_at >= cutoff) for w in workers],
        recent_activity=[ActivityOut.model_validate(e) for e in events],
    )


health = APIRouter(prefix="/health", tags=["health"])


@health.get("/live")
async def live():
    return {"status": "ok"}


@health.get("/ready")
async def ready(db: Db):
    await db.execute(text("SELECT 1"))
    online = await db.scalar(select(func.count()).where(Worker.last_seen_at >= datetime.now(UTC) - WORKER_ONLINE_WINDOW))
    return {"status": "ok", "workers_online": online}


@router.get("/activity")
async def activity(_: auth.CurrentPrincipal, db: Db, level: str | None = None, run_id: int | None = None,
                   before_id: int | None = None, limit: int = 100):
    """Activity / error log (level=error for the System errors view)."""
    q = select(ActivityEvent).order_by(ActivityEvent.id.desc()).limit(min(max(limit, 1), 500))
    if level:
        q = q.where(ActivityEvent.level == level)
    if run_id:
        q = q.where(ActivityEvent.run_id == run_id)
    if before_id:
        q = q.where(ActivityEvent.id < before_id)
    return [{"id": e.id, "ts": e.ts, "level": e.level, "type": e.type, "source": e.source, "message": e.message,
             "run_id": e.run_id, "scan_id": (e.data or {}).get("scan_id"), "request_id": (e.data or {}).get("request_id")}
            for e in (await db.execute(q)).scalars().all()]
