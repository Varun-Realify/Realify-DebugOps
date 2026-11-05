"""Open a pull request for a validated fix using the GitHub Git Data API.

Runs in the worker (never in a sandbox). Creates blobs -> tree (on the base commit) -> commit -> a new branch
under `debugops/`, then the PR. It never pushes to an existing branch it did not create, and never merges.
"""

import base64
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import audit
from app.events import log_event
from app.github.client import GitHubApp, GitHubError
from app.models import Approval, Bug, FixAttempt, GitHubInstallation, PullRequest, Report, Repository, WorkflowRun
from app.schemas import RepoSettings

BRANCH_PREFIX = "debugops/"


class PRError(Exception):
    pass


async def open_pull_request(db: AsyncSession, gh: GitHubApp, run: WorkflowRun, state: dict) -> dict:
    existing = (await db.execute(select(PullRequest).where(PullRequest.run_id == run.id))).scalar_one_or_none()
    if existing:  # idempotent on resume
        return {"number": existing.number, "url": existing.url, "branch": existing.head_branch}
    repo = await db.get(Repository, run.repository_id)
    inst = await db.get(GitHubInstallation, repo.installation_id)
    bug = await db.get(Bug, run.bug_id)
    fa = await db.get(FixAttempt, state["attempt_id"])
    report = (await db.execute(select(Report).where(Report.run_id == run.id))).scalar_one()
    if fa.validation.get("verdict") != "verified" or fa.status != "accepted":
        raise PRError("refusing to open a PR for a fix that is not validated and approved")
    missing = [p for p in ("contents", "pull_requests") if inst.permissions.get(p) != "write"]
    if missing:
        raise PRError(f"GitHub App lacks write permission for: {', '.join(missing)} (see docs/GITHUB_APP.md)")
    branch = run.branch_name
    if not branch.startswith(BRANCH_PREFIX) or branch == repo.default_branch:
        raise PRError(f"refusing to write to branch {branch!r}")

    perms = {"contents": "write", "pull_requests": "write", "metadata": "read"}
    if inst.permissions.get("issues") == "write":
        perms["issues"] = "write"
    token = await gh.installation_token(inst.installation_id, repository_ids=[repo.github_repo_id], permissions=perms)
    full = repo.full_name

    async def post(path: str, body: dict):
        return await gh._send("POST", f"/repos/{full}{path}", auth=f"token {token}", json=body)

    base_tree = (await gh.get(f"/repos/{full}/git/commits/{run.base_sha}", token))["tree"]["sha"]
    listing = await gh.get(f"/repos/{full}/git/trees/{base_tree}", token, params={"recursive": "1"})
    modes = {e["path"]: e["mode"] for e in listing.get("tree", []) if e.get("type") == "blob"}  # keep executable bits
    tree = []
    for path, content in fa.files.items():
        mode = modes.get(path, "100644")
        if content is None:
            tree.append({"path": path, "mode": mode, "type": "blob", "sha": None})
        else:
            blob = await post("/git/blobs", {"content": base64.b64encode(content.encode()).decode(), "encoding": "base64"})
            tree.append({"path": path, "mode": mode, "type": "blob", "sha": blob["sha"]})
    new_tree = await post("/git/trees", {"base_tree": base_tree, "tree": tree})
    from app.workflow.runner import task_id
    message = f"fix: {bug.title}"[:72] + f"\n\nAutomated fix proposed by Realify DebugOps (run #{run.id}, {task_id(run.id)}).\n" \
              f"Validation: {fa.validation.get('reason', '')}\n"
    commit = await post("/git/commits", {"message": message, "tree": new_tree["sha"], "parents": [run.base_sha]})
    try:
        await post("/git/refs", {"ref": f"refs/heads/{branch}", "sha": commit["sha"]})
    except GitHubError as e:
        if e.status != 422:
            raise
        # Branch already exists on GitHub. Only take it over if its tip is DebugOps' own commit for THIS run
        # (a resume after a crash). Anything else may be someone's work: never overwrite it.
        ref = await gh.get(f"/repos/{full}/git/ref/heads/{branch}", token)
        tip = await gh.get(f"/repos/{full}/git/commits/{ref['object']['sha']}", token)
        import re
        if not re.search(rf"Realify DebugOps \(run #{run.id}[,)]", tip.get("message") or ""):
            raise PRError(f"branch {branch} already exists on GitHub and was not created by this run; refusing to overwrite it")
        await gh._send("PATCH", f"/repos/{full}/git/refs/heads/{branch}", auth=f"token {token}",
                       json={"sha": commit["sha"], "force": True})

    approval = (await db.execute(select(Approval).where(Approval.run_id == run.id, Approval.decision == "approve")
                                 .order_by(Approval.id.desc()).limit(1))).scalar_one_or_none()
    body = report.markdown
    if bug.github_issue_number:
        body = f"Fixes #{bug.github_issue_number}\n\n" + body
    if approval:
        body += f"\n\n_PR creation approved in DebugOps by user #{approval.user_id}._"
    pr = await post("/pulls", {"title": f"[DebugOps {task_id(run.id)}] {bug.title}"[:256], "head": branch, "base": repo.default_branch,
                               "body": body[:65000], "maintainer_can_modify": True, "draft": False})
    row = PullRequest(run_id=run.id, repository_id=repo.id, number=pr["number"], url=pr["html_url"], head_branch=branch,
                      head_sha=commit["sha"], state="open", ci_status="pending", checks=[])
    db.add(row)
    run.status = "awaiting_merge"
    run.pr_url = pr["html_url"]
    bug.status = "pr_open"

    if bug.github_issue_number and "issues" in perms and RepoSettings.of(repo.settings).comment_on_issue:
        try:
            await post(f"/issues/{bug.github_issue_number}/comments",
                       {"body": f"Realify DebugOps proposed a fix in #{pr['number']} (validated: {fa.validation.get('reason', '')}). "
                                "A human must review and merge it."})
        except GitHubError:
            log_event(db, "github", f"Could not comment on issue #{bug.github_issue_number}", level="warning", run_id=run.id)
    audit(db, "pull_request.opened", actor_type="system", target_type="workflow_run", target_id=run.id,
          repository=full, number=pr["number"], branch=branch)
    log_event(db, "github", f"Opened PR #{pr['number']} on {full} from {branch}", run_id=run.id)
    return {"number": pr["number"], "url": pr["html_url"], "branch": branch, "opened_at": datetime.now(UTC).isoformat()}


def aggregate_checks(check_runs: list[dict]) -> str:
    if not check_runs:
        return "none"
    if any(c.get("status") != "completed" for c in check_runs):
        return "pending"
    if any(c.get("conclusion") in ("failure", "cancelled", "timed_out", "action_required", "startup_failure") for c in check_runs):
        return "failure"
    return "success"


async def refresh_pull_request(db: AsyncSession, gh: GitHubApp, pr: PullRequest) -> PullRequest:
    """Pull state + CI checks from GitHub (fallback when webhooks are missed)."""
    repo = await db.get(Repository, pr.repository_id)
    inst = await db.get(GitHubInstallation, repo.installation_id)
    token = await gh.installation_token(inst.installation_id, repository_ids=[repo.github_repo_id],
                                        permissions={"pull_requests": "read", "checks": "read", "metadata": "read"}
                                        if inst.permissions.get("checks") else {"pull_requests": "read", "metadata": "read"})
    data = await gh.get(f"/repos/{repo.full_name}/pulls/{pr.number}", token)
    pr.head_sha = data["head"]["sha"]
    if inst.permissions.get("checks"):
        runs = (await gh.get(f"/repos/{repo.full_name}/commits/{pr.head_sha}/check-runs", token, params={"per_page": 100})).get("check_runs", [])
        pr.checks = [{"name": c["name"], "status": c["status"], "conclusion": c.get("conclusion"), "url": c.get("html_url")} for c in runs]
        pr.ci_status = aggregate_checks(pr.checks)
    if data.get("merged") and data.get("merge_commit_sha") and not pr.merge_commit_sha:
        pr.merge_commit_sha, pr.post_merge_ci_status = data["merge_commit_sha"], "pending"
    await apply_pr_state(db, pr, merged=bool(data.get("merged")), state=data["state"], merged_at=data.get("merged_at"))
    return pr


async def apply_pr_state(db: AsyncSession, pr: PullRequest, *, merged: bool, state: str, merged_at: str | None) -> None:
    run = await db.get(WorkflowRun, pr.run_id)
    bug = await db.get(Bug, run.bug_id)
    if merged and pr.state != "merged":
        pr.state, pr.merged_at = "merged", datetime.fromisoformat(merged_at.replace("Z", "+00:00")) if merged_at else datetime.now(UTC)
        run.status, run.finished_at = "succeeded", datetime.now(UTC)
        bug.status = "resolved"  # merged by a human AND validated by DebugOps
        log_event(db, "github", f"PR #{pr.number} merged by a human; bug #{bug.id} resolved", run_id=run.id)
    elif state == "closed" and not merged and pr.state == "open":
        pr.state = "closed"
        run.status, run.finished_at = "closed", datetime.now(UTC)
        bug.status = "triaged"
        log_event(db, "github", f"PR #{pr.number} closed without merging", level="warning", run_id=run.id)
