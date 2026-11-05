"""After the PR: reviews, merge, CI on the merge commit, deployments by Realify's CD, production health.

DebugOps observes; it never merges or deploys. A production health check that keeps failing after a deploy of a
DebugOps fix opens a critical regression bug, which re-enters the same debugging workflow.
"""

import logging
import time
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import queue
from app.audit import audit
from app.detection.parsers import fingerprint, redact
from app.events import log_event
from app.models import Bug, Evidence, PullRequest, Repository, WorkflowRun
from app.schemas import RepoSettings

log = logging.getLogger("debugops.delivery")
MAX_HEALTH_SAMPLES = 60


async def pr_for_repo(db: AsyncSession, repo_gid: int, number: int) -> PullRequest | None:
    return (await db.execute(select(PullRequest).join(Repository, Repository.id == PullRequest.repository_id)
                             .where(Repository.github_repo_id == repo_gid, PullRequest.number == number))).scalar_one_or_none()


def record_review(pr: PullRequest, review: dict) -> None:
    state = (review.get("state") or "").lower()
    entry = {"user": (review.get("user") or {}).get("login"), "state": state, "at": review.get("submitted_at")}
    pr.reviews = [*pr.reviews, entry][-50:]
    if state in ("approved", "changes_requested"):
        pr.review_state = state  # latest decisive review wins
    elif not pr.review_state:
        pr.review_state = "commented"


async def prs_for_sha(db: AsyncSession, sha: str) -> list[PullRequest]:
    return (await db.execute(select(PullRequest).where(or_(PullRequest.merge_commit_sha == sha, PullRequest.head_sha == sha)))).scalars().all()


async def record_deployment(db: AsyncSession, payload: dict) -> bool:
    """deployment / deployment_status webhook -> deployment history on the PR whose merge commit was deployed."""
    dep = payload.get("deployment") or {}
    status = payload.get("deployment_status") or {}
    rows = [pr for pr in await prs_for_sha(db, dep.get("sha", "")) if pr.merge_commit_sha == dep.get("sha")]
    if not rows:
        return False
    entry = {"id": dep.get("id"), "environment": dep.get("environment"), "state": status.get("state") or "created",
             "url": status.get("environment_url") or status.get("target_url") or "", "at": status.get("created_at") or dep.get("created_at")}
    for pr in rows:
        pr.deployments = [d for d in pr.deployments if d.get("id") != entry["id"]] + [entry]
        run = await db.get(WorkflowRun, pr.run_id)
        log_event(db, "delivery", f"{run.task_title[:80]}: deployment to {entry['environment']} is {entry['state']}", run_id=run.id)
        repo = await db.get(Repository, pr.repository_id)
        cfg = RepoSettings.of(repo.settings)
        if entry["state"] == "success" and entry["environment"] == cfg.production_environment and cfg.health_check_url \
                and pr.health.get("deployment_id") != entry["id"]:
            now = datetime.now(UTC)
            pr.health = {"status": "monitoring", "url": cfg.health_check_url, "deployment_id": entry["id"], "checks": [],
                         "consecutive_failures": 0, "started_at": now.isoformat(),
                         "until": (now + timedelta(minutes=cfg.health_check_minutes)).isoformat()}
            await queue.enqueue(db, "delivery.health_check", {"pr_id": pr.id}, max_attempts=1)
            log_event(db, "delivery", f"Monitoring production health for {cfg.health_check_minutes} min after deploy of PR #{pr.number}", run_id=run.id)
    return True


async def probe(url: str, timeout: float = 10.0) -> dict:
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as c:
            r = await c.get(url, headers={"User-Agent": "realify-debugops-healthcheck"})
        ok = 200 <= r.status_code < 300
        return {"at": datetime.now(UTC).isoformat(), "ok": ok, "status": r.status_code, "ms": int((time.monotonic() - t0) * 1000)}
    except httpx.HTTPError as e:
        return {"at": datetime.now(UTC).isoformat(), "ok": False, "status": None, "error": type(e).__name__,
                "ms": int((time.monotonic() - t0) * 1000)}


async def health_check(db: AsyncSession, pr_id: int) -> None:
    """One probe; reschedules itself until the window ends. Repeated failure opens a regression bug."""
    pr = await db.get(PullRequest, pr_id)
    h = dict(pr.health or {})
    if h.get("status") != "monitoring":
        return
    repo = await db.get(Repository, pr.repository_id)
    cfg = RepoSettings.of(repo.settings)
    result = await probe(h["url"])
    h["checks"] = [*h.get("checks", []), result][-MAX_HEALTH_SAMPLES:]
    h["consecutive_failures"] = 0 if result["ok"] else h.get("consecutive_failures", 0) + 1
    run = await db.get(WorkflowRun, pr.run_id)
    if h["consecutive_failures"] >= cfg.health_failure_threshold:
        h["status"] = "unhealthy"
        bug = await open_regression_bug(db, pr, run, repo, h)
        h["bug_id"] = bug.id
        if cfg.investigate_regressions:
            from app.workflow.runner import start_run
            pr.health = h
            await db.commit()
            await start_run(db, bug, None)
            return
    elif datetime.now(UTC) >= datetime.fromisoformat(h["until"]):
        h["status"] = "healthy"
        log_event(db, "delivery", f"Production healthy for the whole window after deploy of PR #{pr.number}", run_id=run.id)
    else:
        await queue.enqueue(db, "delivery.health_check", {"pr_id": pr.id}, max_attempts=1,
                            delay=timedelta(seconds=cfg.health_check_interval_seconds))
    pr.health = h
    await db.commit()


async def open_regression_bug(db: AsyncSession, pr: PullRequest, run: WorkflowRun, repo: Repository, h: dict) -> Bug:
    fp = fingerprint("health", str(repo.id), str(h.get("deployment_id")))
    bug = (await db.execute(select(Bug).where(Bug.repository_id == repo.id, Bug.fingerprint == fp))).scalar_one_or_none()
    if bug:
        return bug
    failures = [c for c in h["checks"] if not c["ok"]][-10:]
    bug = Bug(repository_id=repo.id, source="auto_health", severity="critical", status="new", fingerprint=fp,
              title=f"Production health check failing after deploy of PR #{pr.number}"[:500],
              description=(f"After the deployment of PR #{pr.number} ({pr.url}), which shipped the DebugOps fix for "
                           f"\"{run.task_title}\", the health check {h['url']} failed {h['consecutive_failures']} times in a row. "
                           "The deployment may have introduced a regression; consider rolling back per your release process."),
              affected_files=[{"path": p, "line_start": None, "line_end": None} for p in run.files_changed if p],
              data={"pull_request": pr.url, "run_id": run.id, "deployment_id": h.get("deployment_id")})
    db.add(bug)
    await db.flush()
    db.add(Evidence(bug_id=bug.id, kind="log", content=redact("\n".join(
        f"{c['at']} status={c.get('status')} {c.get('error', '')} {c['ms']}ms" for c in failures))))
    audit(db, "bug.regression_detected", actor_type="system", target_type="bug", target_id=bug.id, pull_request=pr.url)
    log_event(db, "delivery", f"REGRESSION: production health failing after deploy of PR #{pr.number}; opened bug #{bug.id}",
              level="error", type="error", run_id=run.id)
    return bug
