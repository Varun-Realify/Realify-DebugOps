"""Webhook verification and dispatch."""

import hashlib
import hmac

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import audit
from app.github import sync
from app.models import Bug, GitHubInstallation, Repository

# Events later phases act on. Stored now so nothing is lost; processed from the table later.
STORED_EVENTS = {"issue_comment"}


class Ignored(Exception):
    """Event is valid but not for us (repository not enabled, label filter, ...)."""


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    if not secret or not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


async def _installation(db: AsyncSession, payload: dict, action: str | None) -> None:
    data = payload["installation"]
    if action == "deleted":
        await sync.mark_installation_deleted(db, data["id"])
    else:  # created, suspend, unsuspend, new_permissions_accepted
        inst = await sync.upsert_installation(db, data)
        for r in payload.get("repositories") or []:
            await sync.upsert_repo(db, inst, r)
    audit(db, f"github.installation.{action}", actor_type="system", target_type="github_installation",
          target_id=data["id"], account=data["account"]["login"])


async def _installation_repositories(db: AsyncSession, payload: dict, action: str | None) -> None:
    inst = await sync.upsert_installation(db, payload["installation"])
    for r in payload.get("repositories_added") or []:
        await sync.upsert_repo(db, inst, r)
    removed = payload.get("repositories_removed") or []
    await sync.mark_repos_removed(db, [r["id"] for r in removed])
    audit(db, f"github.repositories.{action}", actor_type="system", target_type="github_installation",
          target_id=inst.installation_id,
          added=[r["full_name"] for r in payload.get("repositories_added") or []],
          removed=[r["full_name"] for r in removed])


async def _repository(db: AsyncSession, payload: dict, action: str | None) -> None:
    data = payload["repository"]
    repo = (await db.execute(select(Repository).where(Repository.github_repo_id == data["id"]))).scalar_one_or_none()
    if repo is None:
        return
    if action == "deleted":
        await sync.mark_repos_removed(db, [data["id"]])
        return
    repo.full_name = data["full_name"]
    repo.private = data.get("private", repo.private)
    repo.default_branch = data.get("default_branch") or repo.default_branch


async def _enabled_repo(db: AsyncSession, payload: dict) -> Repository:
    gid = (payload.get("repository") or {}).get("id")
    repo = (await db.execute(select(Repository).where(Repository.github_repo_id == gid))).scalar_one_or_none() if gid else None
    if repo is None or not repo.enabled or not repo.accessible:
        raise Ignored()
    return repo


async def _issues(db: AsyncSession, payload: dict, action: str | None) -> None:
    from app.config import get_settings
    from app.detection.bugs import ingest_issue, wants_issue
    from app.schemas import RepoSettings

    repo = await _enabled_repo(db, payload)
    issue = payload["issue"]
    if action not in ("opened", "edited", "reopened", "labeled", "closed"):
        raise Ignored()
    known = await db.scalar(select(Bug.id).where(Bug.repository_id == repo.id, Bug.github_issue_number == issue["number"]))
    # Already-tracked issues get updates (edit/close) even if labels changed; new ones must pass the filter.
    if not known and not wants_issue(RepoSettings.of(repo.settings), issue, get_settings().github_app_slug):
        raise Ignored()
    if await ingest_issue(db, repo, issue, action) is None:
        raise Ignored()


async def _push(db: AsyncSession, payload: dict, action: str | None) -> None:
    from app.detection.scan import request_scan
    from app.schemas import RepoSettings

    repo = await _enabled_repo(db, payload)
    if payload.get("ref") != f"refs/heads/{repo.default_branch}" or payload.get("deleted"):
        raise Ignored()
    if not RepoSettings.of(repo.settings).scan_on_push:
        raise Ignored()
    await db.commit()  # request_scan manages its own transaction
    await request_scan(db, repo, "push")


async def _pull_request(db: AsyncSession, payload: dict, action: str | None) -> None:
    from app.models import PullRequest
    from app.workflow.pr import apply_pr_state

    pr_data = payload.get("pull_request") or {}
    repo_gid = (payload.get("repository") or {}).get("id")
    row = (await db.execute(select(PullRequest).join(Repository, Repository.id == PullRequest.repository_id)
                            .where(Repository.github_repo_id == repo_gid, PullRequest.number == pr_data.get("number")))).scalar_one_or_none()
    if row is None:
        raise Ignored()  # not a DebugOps PR
    row.head_sha = (pr_data.get("head") or {}).get("sha", row.head_sha)
    if action == "closed":
        if pr_data.get("merged") and pr_data.get("merge_commit_sha"):
            row.merge_commit_sha = pr_data["merge_commit_sha"]  # CI and deployments of this commit are tracked next
            row.post_merge_ci_status = "pending"
        await apply_pr_state(db, row, merged=bool(pr_data.get("merged")), state="closed", merged_at=pr_data.get("merged_at"))


async def _pull_request_review(db: AsyncSession, payload: dict, action: str | None) -> None:
    from app.workflow.delivery import pr_for_repo, record_review

    row = await pr_for_repo(db, (payload.get("repository") or {}).get("id"), (payload.get("pull_request") or {}).get("number"))
    if row is None or action != "submitted":
        raise Ignored()
    record_review(row, payload.get("review") or {})


async def _deployment(db: AsyncSession, payload: dict, action: str | None) -> None:
    from app.workflow.delivery import record_deployment

    if not await record_deployment(db, payload):
        raise Ignored()


async def _checks(db: AsyncSession, payload: dict, action: str | None) -> None:
    from app.models import PullRequest
    from app.workflow.pr import aggregate_checks

    from sqlalchemy import or_

    item = payload.get("check_run") or payload.get("check_suite") or {}
    sha = item.get("head_sha")
    rows = (await db.execute(select(PullRequest).where(or_(PullRequest.head_sha == sha, PullRequest.merge_commit_sha == sha)))).scalars().all() if sha else []
    if not rows:
        raise Ignored()
    for row in rows:
        post_merge = row.merge_commit_sha == sha  # CI / build checks on the default branch after the merge
        checks = row.post_merge_checks if post_merge else row.checks
        if "check_run" in payload:
            entry = {"name": item.get("name"), "status": item.get("status"), "conclusion": item.get("conclusion"), "url": item.get("html_url")}
            checks = [c for c in checks if c.get("name") != entry["name"]] + [entry]
            status_ = aggregate_checks(checks)
        elif item.get("status") == "completed" and not checks:
            status_ = "success" if item.get("conclusion") == "success" else "failure"
        else:
            continue
        if post_merge:
            row.post_merge_checks, row.post_merge_ci_status = checks, status_
        elif row.state == "open":
            row.checks, row.ci_status = checks, status_


HANDLERS = {
    "pull_request": _pull_request,
    "pull_request_review": _pull_request_review,
    "deployment": _deployment,
    "deployment_status": _deployment,
    "check_run": _checks,
    "check_suite": _checks,
    "issues": _issues,
    "push": _push,
    "installation": _installation,
    "installation_repositories": _installation_repositories,
    "repository": _repository,
    "ping": None,
}


async def dispatch(db: AsyncSession, event: str, action: str | None, payload: dict) -> str:
    """Returns the delivery status. Raises on handler failure (caller records 'error')."""
    if event in HANDLERS:
        fn = HANDLERS[event]
        try:
            if fn:
                await fn(db, payload, action)
        except Ignored:
            return "ignored"
        return "processed"
    if event in STORED_EVENTS:
        return "stored"
    return "ignored"


async def resolve_repository_id(db: AsyncSession, payload: dict) -> int | None:
    gid = (payload.get("repository") or {}).get("id")
    if gid is None:
        return None
    return await db.scalar(select(Repository.id).where(Repository.github_repo_id == gid))


async def installation_active(db: AsyncSession, inst_pk: int) -> bool:
    inst = await db.get(GitHubInstallation, inst_pk)
    return bool(inst and not inst.deleted_at and not inst.suspended_at)
