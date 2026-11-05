from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth, queue
from app.audit import audit
from app.config import get_settings
from app.db import get_db
from app.events import log_event
from app.github.client import GitHubApp, GitHubError, get_github
from app.models import (ActivityEvent, AgentStep, Approval, Bug, FixAttempt, GitHubInstallation, PullRequest, Report,
                        Repository, Role, TestResult, TestRun, WorkflowRun)
from app.workflow.graph import NODE_ORDER
from app.workflow.pr import refresh_pull_request
from app.workflow.runner import TERMINAL, start_run, task_id

router = APIRouter(tags=["runs"])
Db = Annotated[AsyncSession, Depends(get_db)]
Gh = Annotated[GitHubApp, Depends(get_github)]
Dev = Annotated[auth.Principal, auth.require_role(Role.admin, Role.developer)]
Approver = Annotated[auth.Principal, auth.require_role(Role.admin, Role.reviewer)]


class DecisionIn(BaseModel):
    reason: str = Field(default="", max_length=2000)


def _run_summary(r: WorkflowRun, bug_title: str, repo: str) -> dict:
    """The run's audit record: everything needed to answer 'what happened on this task' from PostgreSQL."""
    return {"id": r.id, "task_id": task_id(r.id), "task_type": r.task_type, "task_title": r.task_title or bug_title,
            "task_description": r.task_description, "base_sha": r.base_sha, "branch_name": r.branch_name,
            "files_changed": r.files_changed, "test_summary": r.test_summary, "execution_seconds": r.execution_seconds,
            "pr_url": r.pr_url, "bug_id": r.bug_id, "bug_title": bug_title, "repository_id": r.repository_id,
            "repository": r.repository_full_name or repo,
            "status": r.status, "current_node": r.current_node, "attempt": r.attempt, "validation_verdict": r.validation_verdict,
            "started_by": r.started_by, "created_at": r.created_at, "started_at": r.started_at, "finished_at": r.finished_at,
            "tokens_in": r.tokens_in, "tokens_out": r.tokens_out, "cost_usd": r.cost_usd, "error": r.error}


async def _load(db: AsyncSession, run_id: int) -> tuple[WorkflowRun, Bug, Repository]:
    row = (await db.execute(select(WorkflowRun, Bug, Repository).join(Bug, Bug.id == WorkflowRun.bug_id)
                            .join(Repository, Repository.id == WorkflowRun.repository_id).where(WorkflowRun.id == run_id))).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run not found")
    return row[0], row[1], row[2]


@router.post("/bugs/{bug_id}/investigate", status_code=201)
async def investigate(bug_id: int, p: Dev, request: Request, db: Db):
    s = get_settings()
    if not s.agents_configured:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "AI agents are not configured (ANTHROPIC_API_KEY)")
    if not s.github_configured:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "GitHub App is not configured")
    bug = await db.get(Bug, bug_id)
    if bug is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Bug not found")
    repo = await db.get(Repository, bug.repository_id)
    inst = await db.get(GitHubInstallation, repo.installation_id)
    if not repo.enabled or not repo.accessible or inst.deleted_at or inst.suspended_at:
        raise HTTPException(status.HTTP_409_CONFLICT, "Repository is not enabled in DebugOps")
    if bug.status in ("duplicate", "wont_fix"):
        raise HTTPException(status.HTTP_409_CONFLICT, f"Bug is marked {bug.status}")
    run = await start_run(db, bug, p.user.id)
    if run is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "This bug already has an active debugging run")
    audit(db, "run.started", actor_user_id=p.user.id, target_type="workflow_run", target_id=run.id,
          ip=auth.client_ip(request), bug_id=bug.id)
    await db.commit()
    return _run_summary(run, bug.title, repo.full_name)


@router.get("/runs")
async def list_runs(_: auth.CurrentPrincipal, db: Db, bug_id: int | None = None, repository_id: int | None = None,
                    status_: str | None = None, limit: int = 100):
    q = (select(WorkflowRun, Bug.title, Repository.full_name).join(Bug, Bug.id == WorkflowRun.bug_id)
         .join(Repository, Repository.id == WorkflowRun.repository_id).order_by(WorkflowRun.id.desc()).limit(min(max(limit, 1), 500)))
    if bug_id:
        q = q.where(WorkflowRun.bug_id == bug_id)
    if repository_id:
        q = q.where(WorkflowRun.repository_id == repository_id)
    if status_:
        q = q.where(WorkflowRun.status == status_)
    return [_run_summary(r, t, n) for r, t, n in (await db.execute(q)).all()]


@router.get("/runs/export.csv", response_class=PlainTextResponse)
async def export_runs(_: auth.CurrentPrincipal, db: Db, limit: int = 5000):
    """Audit trail of debugging runs as CSV (one row per task)."""
    import csv
    import io

    rows = (await db.execute(select(WorkflowRun, Bug.title, Repository.full_name).join(Bug, Bug.id == WorkflowRun.bug_id)
                             .join(Repository, Repository.id == WorkflowRun.repository_id)
                             .order_by(WorkflowRun.id.desc()).limit(min(max(limit, 1), 50000)))).all()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["task_id", "bug", "repository", "base_commit", "branch", "agent_activity", "files_changed", "tests",
                "execution_seconds", "pr_url", "final_status", "validation", "attempts", "cost_usd", "created_at", "finished_at"])
    for r, title, repo in rows:
        steps = (await db.execute(select(AgentStep.agent).where(AgentStep.run_id == r.id).order_by(AgentStep.id))).scalars().all()
        agents = list(dict.fromkeys(a for a in steps if a != "platform"))
        t = r.test_summary or {}
        tests = f"{t.get('passed', 0)} passed, {t.get('failed', 0)} failed, {t.get('error', 0)} errors" if t else ""
        safe = lambda v: ("'" + v) if isinstance(v, str) and v[:1] in "=+-@\t" else v  # noqa: E731  (CSV formula injection)
        w.writerow([safe(x) for x in (task_id(r.id), r.task_title or title, r.repository_full_name or repo, r.base_sha or "",
                                      r.branch_name or "", " → ".join(agents), " ".join(r.files_changed or []), tests,
                                      r.execution_seconds, r.pr_url or "", r.status, r.validation_verdict or "", r.attempt,
                                      f"{r.cost_usd:.4f}", r.created_at.isoformat(), r.finished_at.isoformat() if r.finished_at else "")])
    return PlainTextResponse(buf.getvalue(), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=debugops-runs.csv"})


@router.get("/runs/{run_id}")
async def get_run(run_id: int, _: auth.CurrentPrincipal, db: Db):
    run, bug, repo = await _load(db, run_id)
    steps = (await db.execute(select(AgentStep).where(AgentStep.run_id == run.id).order_by(AgentStep.id))).scalars().all()
    attempts = (await db.execute(select(FixAttempt).where(FixAttempt.run_id == run.id).order_by(FixAttempt.id))).scalars().all()
    approvals = (await db.execute(select(Approval).where(Approval.run_id == run.id).order_by(Approval.id))).scalars().all()
    pr = (await db.execute(select(PullRequest).where(PullRequest.run_id == run.id))).scalar_one_or_none()
    report = (await db.execute(select(Report).where(Report.run_id == run.id))).scalar_one_or_none()
    from app.models import Investigation
    inv = (await db.execute(select(Investigation).where(Investigation.run_id == run.id).order_by(Investigation.id.desc()).limit(1))).scalar_one_or_none()
    return {
        "investigation": None if inv is None else {
            "status": inv.status, "agent_task_id": inv.agent_task_id, "agent_execution_id": inv.agent_execution_id,
            "summary": inv.summary, "recommendations": inv.recommendations, "records": len(inv.records),
            "key_records": [r for r in inv.records if r.get("type") in ("FINDING", "SUMMARY", "SUGGESTION")][-15:],
            "error": inv.error, "started_at": inv.started_at, "finished_at": inv.finished_at},
        **_run_summary(run, bug.title, repo.full_name), "nodes": run.nodes, "node_order": NODE_ORDER,
        "branch_name": run.branch_name, "base_sha": run.base_sha,
        "steps": [{"id": x.id, "node": x.node, "agent": x.agent, "status": x.status, "attempt": x.attempt, "output": x.output,
                   "tokens_in": x.tokens_in, "tokens_out": x.tokens_out, "cost_usd": x.cost_usd, "started_at": x.started_at,
                   "finished_at": x.finished_at, "error": x.error} for x in steps],
        "attempts": [{"id": a.id, "attempt_no": a.attempt_no, "status": a.status, "fix_paths": a.fix_paths, "test_paths": a.test_paths,
                      "validation": a.validation, "security": a.security, "review": a.review, "created_at": a.created_at} for a in attempts],
        "approvals": [{"id": a.id, "kind": a.kind, "user_id": a.user_id, "decision": a.decision, "reason": a.reason,
                       "created_at": a.created_at} for a in approvals],
        "pull_request": None if pr is None else {"id": pr.id, "number": pr.number, "url": pr.url, "state": pr.state,
                                                 "head_branch": pr.head_branch, "ci_status": pr.ci_status, "checks": pr.checks,
                                                 "merged_at": pr.merged_at, "review_state": pr.review_state, "reviews": pr.reviews,
                                                 "merge_commit_sha": pr.merge_commit_sha, "post_merge_ci_status": pr.post_merge_ci_status,
                                                 "post_merge_checks": pr.post_merge_checks, "deployments": pr.deployments,
                                                 "health": {k: v for k, v in (pr.health or {}).items() if k != "checks"} |
                                                           {"recent": (pr.health or {}).get("checks", [])[-10:]}},
        "report": None if report is None else {"root_cause": report.root_cause, "evidence": report.evidence, "affected": report.affected,
                                               "suggested_fix": report.suggested_fix, "test_summary": report.test_summary,
                                               "security_summary": report.security_summary, "review": report.review,
                                               "markdown": report.markdown},
    }


@router.get("/runs/{run_id}/events")
async def run_events(run_id: int, _: auth.CurrentPrincipal, db: Db, after: int = 0, limit: int = 500):
    rows = (await db.execute(select(ActivityEvent).where(ActivityEvent.run_id == run_id, ActivityEvent.id > after)
                             .order_by(ActivityEvent.id).limit(min(max(limit, 1), 2000)))).scalars().all()
    return [{"id": e.id, "ts": e.ts, "level": e.level, "type": e.type, "source": e.source, "message": e.message} for e in rows]


@router.get("/runs/{run_id}/tests")
async def run_tests(run_id: int, _: auth.CurrentPrincipal, db: Db):
    runs = (await db.execute(select(TestRun).where(TestRun.run_id == run_id).order_by(TestRun.id))).scalars().all()
    out = []
    for t in runs:
        reg = (await db.execute(select(TestResult).where(TestResult.test_run_id == t.id))).scalars().all()
        out.append({"id": t.id, "phase": t.phase, "attempt_no": t.attempt_no, "passed": t.passed, "failed": t.failed,
                    "errors": t.errors, "skipped": t.skipped, "result_format": t.result_format, "timed_out": t.timed_out,
                    "command": t.command, "log_tail": t.log_tail,
                    "regression": [{"test_id": r.test_id, "status": r.status} for r in reg if r.is_regression]})
    return out


@router.get("/runs/{run_id}/attempts/{attempt_id}/diff", response_class=PlainTextResponse)
async def attempt_diff(run_id: int, attempt_id: int, _: auth.CurrentPrincipal, db: Db):
    fa = await db.get(FixAttempt, attempt_id)
    if fa is None or fa.run_id != run_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Attempt not found")
    return fa.diff


async def _decide(run_id: int, decision: str, body: DecisionIn, p: auth.Principal, request: Request, db: AsyncSession):
    run, bug, _repo = await _load(db, run_id)
    if run.status != "waiting_approval":
        raise HTTPException(status.HTTP_409_CONFLICT, f"Run is {run.status}, not waiting for approval")
    if run.started_by == p.user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Separation of duties: the user who started a run cannot approve it")
    db.add(Approval(run_id=run.id, kind="open_pr", user_id=p.user.id, decision=decision, reason=body.reason))
    run.status = "queued"
    await queue.enqueue(db, "workflow.run", {"run_id": run.id, "resume": {"decision": decision, "user_id": p.user.id,
                                                                          "reason": body.reason}}, max_attempts=1)
    audit(db, f"run.{decision}d", actor_user_id=p.user.id, target_type="workflow_run", target_id=run.id,
          ip=auth.client_ip(request), reason=body.reason)
    log_event(db, "workflow", f"Run #{run.id}: pull request {decision}d by user #{p.user.id}", run_id=run.id)
    await db.commit()
    return {"status": "queued"}


@router.post("/runs/{run_id}/approve")
async def approve(run_id: int, body: DecisionIn, p: Approver, request: Request, db: Db):
    return await _decide(run_id, "approve", body, p, request, db)


@router.post("/runs/{run_id}/reject")
async def reject(run_id: int, body: DecisionIn, p: Approver, request: Request, db: Db):
    return await _decide(run_id, "reject", body, p, request, db)


@router.post("/runs/{run_id}/cancel")
async def cancel(run_id: int, p: Dev, request: Request, db: Db):
    run, bug, _repo = await _load(db, run_id)
    if run.status in TERMINAL or run.status == "awaiting_merge":
        raise HTTPException(status.HTTP_409_CONFLICT, f"Run is already {run.status}")
    was_waiting = run.status in ("waiting_approval", "queued")
    run.status = "cancelled"
    if was_waiting:  # no worker is executing it: finish now
        run.finished_at = datetime.now(UTC)
        if bug.status == "investigating":
            bug.status = "triaged"
    audit(db, "run.cancelled", actor_user_id=p.user.id, target_type="workflow_run", target_id=run.id, ip=auth.client_ip(request))
    log_event(db, "workflow", f"Run #{run.id} cancelled by user #{p.user.id}" +
              ("" if was_waiting else " (stops after the current step)"), level="warning", run_id=run.id)
    await db.commit()
    return {"status": "cancelled"}


@router.get("/pull-requests")
async def list_prs(_: auth.CurrentPrincipal, db: Db):
    """Proposed fixes: DebugOps PRs (and runs waiting for the optional pre-PR approval) with their review state."""
    out = []
    rows = (await db.execute(select(PullRequest, WorkflowRun).join(WorkflowRun, WorkflowRun.id == PullRequest.run_id)
                             .order_by(PullRequest.id.desc()).limit(300))).all()
    for pr, run in rows:
        waiting = pr.state == "open" and pr.review_state != "approved"
        out.append({"kind": "pull_request", "id": pr.id, "run_id": run.id, "task_id": task_id(run.id), "task_title": run.task_title,
                    "repository": run.repository_full_name, "number": pr.number, "url": pr.url, "state": pr.state,
                    "head_branch": pr.head_branch, "ci_status": pr.ci_status, "checks": pr.checks, "review_state": pr.review_state,
                    "awaiting_developer": waiting, "validation": run.validation_verdict, "files_changed": run.files_changed,
                    "test_summary": run.test_summary, "created_at": pr.created_at, "merged_at": pr.merged_at,
                    "post_merge_ci_status": pr.post_merge_ci_status, "deployments": pr.deployments,
                    "health": (pr.health or {}).get("status")})
    for run in (await db.execute(select(WorkflowRun).where(WorkflowRun.status == "waiting_approval").order_by(WorkflowRun.id.desc()))).scalars():
        out.insert(0, {"kind": "pending_approval", "id": None, "run_id": run.id, "task_id": task_id(run.id), "task_title": run.task_title,
                       "repository": run.repository_full_name, "number": None, "url": None, "state": "awaiting approval in DebugOps",
                       "head_branch": run.branch_name, "ci_status": None, "checks": [], "review_state": None, "awaiting_developer": True,
                       "validation": run.validation_verdict, "files_changed": run.files_changed, "test_summary": run.test_summary,
                       "created_at": run.updated_at, "merged_at": None, "post_merge_ci_status": None, "deployments": [], "health": None})
    return out


@router.post("/pull-requests/{pr_id}/refresh")
async def refresh_pr(pr_id: int, _: Dev, db: Db, gh: Gh):
    pr = await db.get(PullRequest, pr_id)
    if pr is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Pull request not found")
    try:
        await refresh_pull_request(db, gh, pr)
    except GitHubError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e)) from e
    await db.commit()
    return {"state": pr.state, "ci_status": pr.ci_status, "checks": pr.checks}
