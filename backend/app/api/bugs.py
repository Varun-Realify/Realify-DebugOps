from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth, queue
from app.audit import audit
from app.config import get_settings
from app.db import get_db
from app.detection import bugs as bug_ops
from app.detection.scan import request_scan
from app.events import log_event
from app.github.client import GitHubApp, GitHubError, GitHubNotConfigured, get_github
from app.models import Bug, Evidence, GitHubInstallation, Repository, Role, ScanRun, TestResult, TestRun
from app.schemas import Severity

router = APIRouter(tags=["detection"])
Db = Annotated[AsyncSession, Depends(get_db)]
Gh = Annotated[GitHubApp, Depends(get_github)]
Dev = Annotated[auth.Principal, auth.require_role(Role.admin, Role.developer)]

BugStatus = Literal["new", "triaged", "investigating", "fix_proposed", "pr_open", "resolved", "wont_fix", "duplicate", "closed"]


class ScanOut(BaseModel):
    id: int
    repository_id: int
    repository: str
    trigger: str
    status: str
    commit_sha: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    steps: dict
    summary: dict
    error: str | None


class BugOut(BaseModel):
    id: int
    repository_id: int
    repository: str
    source: str
    title: str
    severity: str
    status: str
    affected_files: list
    github_issue_number: int | None
    github_issue_url: str | None
    occurrences: int
    last_seen_at: datetime
    created_at: datetime
    duplicate_of: int | None
    stale: bool  # auto-detected bug not seen in this repository's latest successful scan


class EvidenceOut(BaseModel):
    id: int
    kind: str
    content: str
    file_path: str | None
    line_start: int | None
    line_end: int | None
    commit_sha: str | None
    scan_run_id: int | None
    created_at: datetime


class BugDetail(BugOut):
    description: str
    data: dict
    evidence: list[EvidenceOut]
    similar: list[dict]


class BugPatch(BaseModel):
    status: BugStatus | None = None
    severity: Severity | None = None


class DuplicateIn(BaseModel):
    of: int


class Attachment(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    content: str = Field(max_length=1_000_000)  # text only (logs, traces); read client-side


class BugReportIn(BaseModel):
    """A developer-reported bug (uploaded through DebugOps instead of GitHub)."""
    title: str = Field(min_length=3, max_length=300)
    description: str = Field(default="", max_length=20000)
    steps_to_reproduce: str = Field(default="", max_length=10000)
    stack_trace: str = Field(default="", max_length=50000)
    severity: Severity = "medium"
    affected_files: list[str] = Field(default_factory=list, max_length=20)
    attachments: list[Attachment] = Field(default_factory=list, max_length=5)
    open_github_issue: bool = False
    investigate: bool = False


class TestRunOut(BaseModel):
    id: int
    command: str
    exit_code: int | None
    timed_out: bool
    result_format: str
    passed: int
    failed: int
    errors: int
    skipped: int
    duration_ms: int
    log_tail: str
    results: list[dict] = Field(default_factory=list)


def _scan_out(s: ScanRun, repo_name: str) -> ScanOut:
    return ScanOut(id=s.id, repository_id=s.repository_id, repository=repo_name, trigger=s.trigger, status=s.status,
                   commit_sha=s.commit_sha, created_at=s.created_at, started_at=s.started_at, finished_at=s.finished_at,
                   steps=s.steps, summary=s.summary, error=s.error)


async def _latest_scan_ids(db: AsyncSession) -> dict[int, int]:
    rows = await db.execute(select(ScanRun.repository_id, func.max(ScanRun.id))
                            .where(ScanRun.status.in_(["succeeded", "partial"])).group_by(ScanRun.repository_id))
    return dict(rows.all())


def _bug_out(b: Bug, repo_name: str, latest: dict[int, int]) -> dict:
    stale = b.source != "github_issue" and b.repository_id in latest and b.last_seen_scan_id != latest[b.repository_id]
    return dict(id=b.id, repository_id=b.repository_id, repository=repo_name, source=b.source, title=b.title,
                severity=b.severity, status=b.status, affected_files=b.affected_files,
                github_issue_number=b.github_issue_number, github_issue_url=b.github_issue_url,
                occurrences=b.occurrences, last_seen_at=b.last_seen_at, created_at=b.created_at,
                duplicate_of=b.duplicate_of, stale=stale)


async def _usable_repo(db: AsyncSession, repo_id: int) -> Repository:
    row = (await db.execute(select(Repository, GitHubInstallation)
                            .join(GitHubInstallation, GitHubInstallation.id == Repository.installation_id)
                            .where(Repository.id == repo_id))).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Repository not found")
    repo, inst = row
    if not repo.enabled or not repo.accessible or inst.deleted_at or inst.suspended_at:
        raise HTTPException(status.HTTP_409_CONFLICT, "Repository is not enabled in DebugOps")
    return repo


# ---------------- scans ----------------

@router.post("/repositories/{repo_id}/scans", response_model=ScanOut, status_code=201)
async def start_scan(repo_id: int, p: Dev, request: Request, db: Db):
    repo = await _usable_repo(db, repo_id)
    scan = await request_scan(db, repo, "manual", p.user.id)
    if scan is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "A scan is already queued or running for this repository")
    audit(db, "scan.requested", actor_user_id=p.user.id, target_type="repository", target_id=repo.id,
          ip=auth.client_ip(request), scan_id=scan.id)
    await db.commit()
    return _scan_out(scan, repo.full_name)


@router.post("/repositories/{repo_id}/import-issues", status_code=202)
async def import_issues(repo_id: int, p: Dev, request: Request, db: Db):
    repo = await _usable_repo(db, repo_id)
    await queue.enqueue(db, "github.import_issues", {"repository_id": repo.id})
    audit(db, "issues.import_requested", actor_user_id=p.user.id, target_type="repository", target_id=repo.id,
          ip=auth.client_ip(request))
    await db.commit()
    return {"status": "queued"}


@router.get("/scans", response_model=list[ScanOut])
async def list_scans(_: auth.CurrentPrincipal, db: Db, repository_id: int | None = None, limit: int = 50):
    q = (select(ScanRun, Repository.full_name).join(Repository, Repository.id == ScanRun.repository_id)
         .order_by(ScanRun.id.desc()).limit(min(max(limit, 1), 200)))
    if repository_id:
        q = q.where(ScanRun.repository_id == repository_id)
    return [_scan_out(s, name) for s, name in (await db.execute(q)).all()]


@router.get("/scans/{scan_id}", response_model=ScanOut)
async def get_scan(scan_id: int, _: auth.CurrentPrincipal, db: Db):
    row = (await db.execute(select(ScanRun, Repository.full_name).join(Repository, Repository.id == ScanRun.repository_id)
                            .where(ScanRun.id == scan_id))).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Scan not found")
    return _scan_out(*row)


@router.get("/scans/{scan_id}/tests", response_model=list[TestRunOut])
async def scan_tests(scan_id: int, _: auth.CurrentPrincipal, db: Db):
    runs = (await db.execute(select(TestRun).where(TestRun.scan_run_id == scan_id).order_by(TestRun.id))).scalars().all()
    out = []
    for r in runs:
        results = (await db.execute(select(TestResult).where(TestResult.test_run_id == r.id)
                                    .order_by(TestResult.status != "failed", TestResult.status != "error", TestResult.test_id))).scalars().all()
        out.append(TestRunOut(id=r.id, command=r.command, exit_code=r.exit_code, timed_out=r.timed_out,
                              result_format=r.result_format, passed=r.passed, failed=r.failed, errors=r.errors,
                              skipped=r.skipped, duration_ms=r.duration_ms, log_tail=r.log_tail,
                              results=[{"test_id": t.test_id, "status": t.status, "duration_ms": t.duration_ms,
                                        "message": t.message[:4000]} for t in results]))
    return out


# ---------------- bugs ----------------

@router.get("/bugs", response_model=list[BugOut])
async def list_bugs(_: auth.CurrentPrincipal, db: Db, repository_id: int | None = None, status_: str | None = None,
                    severity: str | None = None, source: str | None = None, q: str | None = None,
                    open_only: bool = False, limit: int = 200):
    query = (select(Bug, Repository.full_name).join(Repository, Repository.id == Bug.repository_id)
             .order_by(Bug.last_seen_at.desc()).limit(min(max(limit, 1), 500)))
    if repository_id:
        query = query.where(Bug.repository_id == repository_id)
    if status_:
        query = query.where(Bug.status == status_)
    if open_only:
        query = query.where(Bug.status.not_in(["resolved", "wont_fix", "duplicate", "closed"]))
    if severity:
        query = query.where(Bug.severity == severity)
    if source:
        query = query.where(Bug.source == source)
    if q:
        like = f"%{q.replace('%', r'\%').replace('_', r'\_')}%"
        query = query.where(or_(Bug.title.ilike(like), Bug.description.ilike(like)))
    latest = await _latest_scan_ids(db)
    return [_bug_out(b, name, latest) for b, name in (await db.execute(query)).all()]


async def _load_bug(db: AsyncSession, bug_id: int) -> tuple[Bug, str]:
    row = (await db.execute(select(Bug, Repository.full_name).join(Repository, Repository.id == Bug.repository_id)
                            .where(Bug.id == bug_id))).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Bug not found")
    return row[0], row[1]


@router.get("/bugs/{bug_id}", response_model=BugDetail)
async def get_bug(bug_id: int, _: auth.CurrentPrincipal, db: Db):
    bug, name = await _load_bug(db, bug_id)
    evidence = (await db.execute(select(Evidence).where(Evidence.bug_id == bug.id).order_by(Evidence.id.desc()).limit(20))).scalars().all()
    similar = [{"id": b.id, "title": b.title, "status": b.status, "source": b.source, "similarity": round(s, 2)}
               for b, s in await bug_ops.similar_bugs(db, bug)]
    return BugDetail(**_bug_out(bug, name, await _latest_scan_ids(db)), description=bug.description, data=bug.data,
                     evidence=[EvidenceOut.model_validate(e, from_attributes=True) for e in evidence], similar=similar)


@router.post("/repositories/{repo_id}/bugs", status_code=201)
async def report_bug(repo_id: int, body: BugReportIn, p: Dev, request: Request, db: Db, gh: Gh):
    import uuid

    from app.detection.parsers import fingerprint, redact
    from app.github.client import safe_repo_path
    from app.workflow.runner import start_run

    repo = await _usable_repo(db, repo_id)
    try:
        files = [{"path": safe_repo_path(f), "line_start": None, "line_end": None} for f in body.affected_files if f.strip()]
    except ValueError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"affected_files: {e}") from e
    description = body.description + (f"\n\nSteps to reproduce:\n{body.steps_to_reproduce}" if body.steps_to_reproduce else "")
    bug = Bug(repository_id=repo.id, source="manual", title=body.title.strip(), description=redact(description),
              severity=body.severity, status="new", fingerprint=fingerprint("manual", uuid.uuid4().hex),
              affected_files=files, data={"reported_by": p.user.id, "attachments": [a.name for a in body.attachments]})
    db.add(bug)
    await db.flush()
    db.add(Evidence(bug_id=bug.id, kind="issue", content=redact(f"{bug.title}\n\n{description}")))
    if body.stack_trace.strip():
        db.add(Evidence(bug_id=bug.id, kind="stack_trace", content=redact(body.stack_trace)))
    for a in body.attachments:  # secrets in uploaded logs are redacted before storage
        db.add(Evidence(bug_id=bug.id, kind="log", content=redact(f"[{a.name}]\n{a.content}")[:200000], file_path=None))
    audit(db, "bug.reported", actor_user_id=p.user.id, target_type="bug", target_id=bug.id, ip=auth.client_ip(request),
          repository=repo.full_name, investigate=body.investigate)
    log_event(db, "bugs", f"Bug #{bug.id} reported by user #{p.user.id} in {repo.full_name}: {bug.title}")
    await db.commit()

    result: dict = {"bug_id": bug.id, "similar": [{"id": b.id, "title": b.title, "status": b.status, "similarity": round(sv, 2)}
                                                  for b, sv in await bug_ops.similar_bugs(db, bug)]}
    if body.open_github_issue:
        try:
            await bug_ops.create_github_issue(db, gh, bug, actor_user_id=p.user.id, web_url=get_settings().github_web_url)
            await db.commit()
            result["github_issue_url"] = bug.github_issue_url
        except (bug_ops.IssueCreationRefused, GitHubError, GitHubNotConfigured) as e:
            await db.rollback()
            result["github_issue_error"] = str(e)
    if body.investigate:
        if not get_settings().agents_configured:
            result["run_error"] = "AI agents are not configured (ANTHROPIC_API_KEY)"
        else:
            run = await start_run(db, await db.get(Bug, bug.id), p.user.id)
            result["run_id"] = run.id if run else None
    return result


@router.patch("/bugs/{bug_id}", response_model=BugOut)
async def update_bug(bug_id: int, body: BugPatch, p: Dev, request: Request, db: Db):
    bug, name = await _load_bug(db, bug_id)
    changes = body.model_dump(exclude_unset=True, exclude_none=True)
    if changes.get("status") == "duplicate":
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Use mark-duplicate to set a duplicate")
    for k, v in changes.items():
        setattr(bug, k, v)
    audit(db, "bug.updated", actor_user_id=p.user.id, target_type="bug", target_id=bug.id, ip=auth.client_ip(request), changes=changes)
    await db.commit()
    return _bug_out(bug, name, await _latest_scan_ids(db))


@router.post("/bugs/{bug_id}/mark-duplicate", response_model=BugOut)
async def mark_duplicate(bug_id: int, body: DuplicateIn, p: Dev, request: Request, db: Db):
    bug, name = await _load_bug(db, bug_id)
    target = await db.get(Bug, body.of)
    if target is None or target.repository_id != bug.repository_id or target.id == bug.id:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Duplicate target must be another bug in the same repository")
    if target.duplicate_of == bug.id:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "That bug is already a duplicate of this one")
    bug.status, bug.duplicate_of = "duplicate", target.id
    audit(db, "bug.marked_duplicate", actor_user_id=p.user.id, target_type="bug", target_id=bug.id,
          ip=auth.client_ip(request), duplicate_of=target.id)
    await db.commit()
    return _bug_out(bug, name, await _latest_scan_ids(db))


@router.post("/bugs/{bug_id}/github-issue", response_model=BugOut)
async def open_github_issue(bug_id: int, p: Dev, db: Db, gh: Gh):
    bug, name = await _load_bug(db, bug_id)
    try:
        await bug_ops.create_github_issue(db, gh, bug, actor_user_id=p.user.id, web_url=get_settings().github_web_url)
    except bug_ops.IssueCreationRefused as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from e
    except GitHubNotConfigured as e:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "GitHub App is not configured") from e
    except GitHubError as e:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(e)) from e
    await db.commit()
    return _bug_out(bug, name, await _latest_scan_ids(db))
