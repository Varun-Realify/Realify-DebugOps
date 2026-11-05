"""Worker job handlers and the scan scheduler. Imported by app.worker to register handlers."""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.detection import bugs, scan
from app.events import log_event
from app.github.client import get_github
from app.models import Bug, GitHubInstallation, Job, Repository, ScanRun
from app.schemas import RepoSettings
from app.worker import handler

log = logging.getLogger("debugops.jobs")
MAX_IMPORT = 1000


@handler("scan.run")
async def run_scan(db: AsyncSession, job: Job) -> None:
    await scan.execute(db, job.payload["scan_id"], get_github())


@handler("github.create_issue")
async def create_issue(db: AsyncSession, job: Job) -> None:
    bug = await db.get(Bug, job.payload["bug_id"])
    if bug is None or bug.github_issue_number:
        return
    try:
        await bugs.create_github_issue(db, get_github(), bug, actor_user_id=None, web_url=get_settings().github_web_url)
    except bugs.IssueCreationRefused as e:
        log_event(db, "github", f"Did not open an issue for bug #{bug.id}: {e}", level="warning")
    await db.commit()


@handler("github.import_issues")
async def import_issues(db: AsyncSession, job: Job) -> None:
    """Backfill: ingest currently open issues that match the repository's label filter."""
    repo = await db.get(Repository, job.payload["repository_id"])
    inst = await db.get(GitHubInstallation, repo.installation_id)
    if not repo.enabled:
        return
    gh = get_github()
    token = await gh.installation_token(inst.installation_id, repository_ids=[repo.github_repo_id],
                                        permissions={"issues": "read", "metadata": "read"})
    issues = await gh.paginate(f"/repos/{repo.full_name}/issues", token=token, params={"state": "open"})
    cfg, slug = RepoSettings.of(repo.settings), get_settings().github_app_slug
    count = 0
    for issue in issues[:MAX_IMPORT]:
        if bugs.wants_issue(cfg, issue, slug) and await bugs.ingest_issue(db, repo, issue):
            count += 1
    log_event(db, "github", f"Imported {count} open issue(s) from {repo.full_name}")
    await db.commit()


async def enqueue_scheduled_scans(db: AsyncSession) -> int:
    """Queue scans for repositories whose scan_interval_hours has elapsed since their last scan."""
    queued = 0
    repos = (await db.execute(select(Repository).join(GitHubInstallation, GitHubInstallation.id == Repository.installation_id)
                              .where(Repository.enabled.is_(True), Repository.accessible.is_(True),
                                     GitHubInstallation.deleted_at.is_(None), GitHubInstallation.suspended_at.is_(None)))).scalars().all()
    for repo in repos:
        hours = RepoSettings.of(repo.settings).scan_interval_hours
        if not hours:
            continue
        last = await db.scalar(select(func.max(ScanRun.created_at)).where(ScanRun.repository_id == repo.id))
        if last is None or last < datetime.now(UTC) - timedelta(hours=hours):
            if await scan.request_scan(db, repo, "schedule"):
                queued += 1
    return queued
