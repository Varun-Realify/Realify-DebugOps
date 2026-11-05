"""Keep github_installations / repositories in step with GitHub (via webhooks or a full sync)."""

from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.github.client import GitHubApp
from app.models import GitHubInstallation, Repository


def _ts(v: str | None) -> datetime | None:
    return datetime.fromisoformat(v.replace("Z", "+00:00")) if v else None


async def upsert_installation(db: AsyncSession, data: dict) -> GitHubInstallation:
    inst = (await db.execute(select(GitHubInstallation).where(GitHubInstallation.installation_id == data["id"]))).scalar_one_or_none()
    if inst is None:
        inst = GitHubInstallation(installation_id=data["id"])
        db.add(inst)
    inst.account_login = data["account"]["login"]
    inst.account_type = data["account"].get("type", "Organization")
    inst.repository_selection = data.get("repository_selection", "selected")
    inst.permissions = data.get("permissions") or {}
    inst.events = data.get("events") or []
    inst.suspended_at = _ts(data.get("suspended_at"))
    inst.deleted_at = None
    await db.flush()
    return inst


async def upsert_repo(db: AsyncSession, inst: GitHubInstallation, data: dict) -> Repository:
    repo = (await db.execute(select(Repository).where(Repository.github_repo_id == data["id"]))).scalar_one_or_none()
    if repo is None:
        repo = Repository(github_repo_id=data["id"], installation_id=inst.id, enabled=False, settings={})
        db.add(repo)
    elif repo.installation_id != inst.id:  # transferred to another installation: require re-enable
        repo.installation_id = inst.id
        repo.enabled = False
    repo.full_name = data["full_name"]
    repo.private = data.get("private", True)
    if data.get("default_branch"):  # not included in installation webhook payloads
        repo.default_branch = data["default_branch"]
    repo.accessible = True
    await db.flush()
    return repo


async def mark_repos_removed(db: AsyncSession, github_repo_ids: list[int]) -> None:
    if github_repo_ids:
        await db.execute(update(Repository).where(Repository.github_repo_id.in_(github_repo_ids))
                         .values(accessible=False, enabled=False))


async def mark_installation_deleted(db: AsyncSession, installation_id: int) -> None:
    inst = (await db.execute(select(GitHubInstallation).where(GitHubInstallation.installation_id == installation_id))).scalar_one_or_none()
    if inst:
        inst.deleted_at = datetime.now(UTC)
        await db.execute(update(Repository).where(Repository.installation_id == inst.id).values(accessible=False, enabled=False))


async def full_sync(db: AsyncSession, gh: GitHubApp) -> dict:
    """Reconcile every installation and its repositories with GitHub. Caller commits."""
    installations = await gh.paginate("/app/installations")
    seen_inst: set[int] = set()
    repo_count = 0
    for data in installations:
        inst = await upsert_installation(db, data)
        seen_inst.add(inst.installation_id)
        if inst.suspended_at:
            continue
        token = await gh.installation_token(inst.installation_id, permissions={"metadata": "read"})
        repos = await gh.paginate("/installation/repositories", token=token, key="repositories")
        seen_repo = {r["id"] for r in repos}
        for r in repos:
            await upsert_repo(db, inst, r)
        repo_count += len(repos)
        gone = (await db.execute(select(Repository.github_repo_id).where(
            Repository.installation_id == inst.id, Repository.github_repo_id.not_in(seen_repo or {0})))).scalars().all()
        await mark_repos_removed(db, list(gone))

    stale = (await db.execute(select(GitHubInstallation.installation_id).where(
        GitHubInstallation.deleted_at.is_(None), GitHubInstallation.installation_id.not_in(seen_inst or {0})))).scalars().all()
    for iid in stale:
        await mark_installation_deleted(db, iid)
    return {"installations": len(installations), "repositories": repo_count, "removed_installations": len(stale)}
