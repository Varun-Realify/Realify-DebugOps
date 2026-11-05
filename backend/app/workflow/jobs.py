"""Worker handlers for debugging runs and pull request tracking."""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.github.client import GitHubError, get_github
from app.models import Job, PullRequest
from app.worker import handler
from app.workflow import runner
from app.workflow.pr import refresh_pull_request

log = logging.getLogger("debugops.workflow.jobs")


@handler("workflow.run")
async def run_workflow(db: AsyncSession, job: Job) -> None:
    await runner.execute(job.payload["run_id"], resume=job.payload.get("resume"))


async def refresh_open_prs(db: AsyncSession) -> int:
    """Fallback for missed webhooks: re-read state and CI checks of open DebugOps PRs."""
    n = 0
    for pr in (await db.execute(select(PullRequest).where(PullRequest.state == "open"))).scalars().all():
        try:
            await refresh_pull_request(db, get_github(), pr)
            n += 1
        except GitHubError as e:
            log.warning("refreshing PR #%s failed: %s", pr.number, e)
    await db.commit()
    return n


@handler("delivery.health_check")
async def delivery_health_check(db: AsyncSession, job: Job) -> None:
    from app.workflow.delivery import health_check
    await health_check(db, job.payload["pr_id"])
