import base64
import logging
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth
from app.audit import audit
from app.config import get_settings
from app.db import get_db
from app.github import sync, webhooks
from app.github.client import GitHubApp, GitHubError, GitHubNotConfigured, contents_url, get_github, safe_ref, safe_repo_path
from app.models import GitHubInstallation, Repository, Role, WebhookDelivery
from app.schemas import RepoSettings

log = logging.getLogger("debugops.github")
router = APIRouter(tags=["github"])
Db = Annotated[AsyncSession, Depends(get_db)]
Gh = Annotated[GitHubApp, Depends(get_github)]
Admin = Annotated[auth.Principal, auth.require_role(Role.admin)]

# Read permissions DebugOps uses in Phase 2. A token never asks for more than the installation granted.
READ_PERMISSIONS = ("metadata", "contents", "issues", "pull_requests", "checks")
MAX_FILE_BYTES = 1_000_000


def _github_error(e: Exception) -> HTTPException:
    if isinstance(e, GitHubNotConfigured):
        return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "GitHub App is not configured")
    assert isinstance(e, GitHubError)
    code = status.HTTP_404_NOT_FOUND if e.status == 404 else status.HTTP_502_BAD_GATEWAY
    return HTTPException(code, e.message if e.status == 404 else str(e))


# ---------------- schemas ----------------

class InstallationOut(BaseModel):
    id: int
    installation_id: int
    account_login: str
    account_type: str
    repository_selection: str
    permissions: dict
    suspended: bool
    deleted: bool


class RepositoryOut(BaseModel):
    id: int
    full_name: str
    default_branch: str
    private: bool
    accessible: bool
    enabled: bool
    installation_account: str
    installation_active: bool
    last_access_check_at: datetime | None
    last_access_ok: bool | None
    settings: RepoSettings


class RepositoryPatch(BaseModel):
    enabled: bool | None = None
    settings: RepoSettings | None = None


def _inst_out(i: GitHubInstallation) -> InstallationOut:
    return InstallationOut(id=i.id, installation_id=i.installation_id, account_login=i.account_login,
                           account_type=i.account_type, repository_selection=i.repository_selection,
                           permissions=i.permissions, suspended=bool(i.suspended_at), deleted=bool(i.deleted_at))


def _repo_out(r: Repository, i: GitHubInstallation) -> RepositoryOut:
    return RepositoryOut(id=r.id, full_name=r.full_name, default_branch=r.default_branch, private=r.private,
                         accessible=r.accessible, enabled=r.enabled, installation_account=i.account_login,
                         installation_active=not i.deleted_at and not i.suspended_at,
                         last_access_check_at=r.last_access_check_at, last_access_ok=r.last_access_ok,
                         settings=RepoSettings.of(r.settings))


async def _load_repo(db: AsyncSession, repo_id: int) -> tuple[Repository, GitHubInstallation]:
    row = (await db.execute(select(Repository, GitHubInstallation)
                            .join(GitHubInstallation, GitHubInstallation.id == Repository.installation_id)
                            .where(Repository.id == repo_id))).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Repository not found")
    return row[0], row[1]


def _read_perms(inst: GitHubInstallation, wanted=READ_PERMISSIONS) -> dict[str, str]:
    return {p: "read" for p in wanted if inst.permissions.get(p) in ("read", "write")}


# ---------------- status / installations ----------------

@router.get("/github/status")
async def github_status(_: auth.CurrentPrincipal, db: Db, gh: Gh):
    s = get_settings()
    last = await db.scalar(select(func.max(WebhookDelivery.received_at)))
    problem = None
    if s.github_configured:
        try:
            gh.app_jwt()  # local check only: key file readable and valid
        except GitHubNotConfigured as e:
            problem = str(e)
    return {
        "configured": s.github_configured,
        "problem": problem,
        "app_id": s.github_app_id or None,
        "install_url": f"{s.github_web_url}/apps/{s.github_app_slug}/installations/new" if s.github_app_slug else None,
        "api_url": s.github_api_url,
        "last_webhook_at": last,
        "installations": await db.scalar(select(func.count()).where(GitHubInstallation.deleted_at.is_(None))),
    }


@router.get("/github/installations", response_model=list[InstallationOut])
async def list_installations(_: auth.CurrentPrincipal, db: Db):
    rows = (await db.execute(select(GitHubInstallation).order_by(GitHubInstallation.account_login))).scalars().all()
    return [_inst_out(i) for i in rows]


@router.post("/github/sync")
async def sync_installations(p: Admin, request: Request, db: Db, gh: Gh):
    try:
        result = await sync.full_sync(db, gh)
    except (GitHubError, GitHubNotConfigured) as e:
        await db.rollback()
        raise _github_error(e) from e
    audit(db, "github.sync", actor_user_id=p.user.id, ip=auth.client_ip(request), **result)
    await db.commit()
    return result


# ---------------- repositories ----------------

@router.get("/repositories", response_model=list[RepositoryOut])
async def list_repositories(_: auth.CurrentPrincipal, db: Db):
    rows = (await db.execute(select(Repository, GitHubInstallation)
                             .join(GitHubInstallation, GitHubInstallation.id == Repository.installation_id)
                             .order_by(Repository.full_name))).all()
    return [_repo_out(r, i) for r, i in rows]


@router.patch("/repositories/{repo_id}", response_model=RepositoryOut)
async def update_repository(repo_id: int, body: RepositoryPatch, p: Admin, request: Request, db: Db):
    repo, inst = await _load_repo(db, repo_id)
    if body.enabled and (not repo.accessible or inst.deleted_at or inst.suspended_at):
        raise HTTPException(status.HTTP_409_CONFLICT, "Repository is not accessible to the GitHub App")
    if body.enabled is not None and body.enabled != repo.enabled:
        repo.enabled = body.enabled
        audit(db, "repository.enabled" if body.enabled else "repository.disabled", actor_user_id=p.user.id,
              target_type="repository", target_id=repo.id, ip=auth.client_ip(request), full_name=repo.full_name)
    if body.settings is not None:
        new = body.settings.model_dump()
        old = RepoSettings.of(repo.settings).model_dump()
        repo.settings = new
        audit(db, "repository.settings_changed", actor_user_id=p.user.id, target_type="repository", target_id=repo.id,
              ip=auth.client_ip(request), changes={k: v for k, v in new.items() if old.get(k) != v})
    await db.commit()
    return _repo_out(repo, inst)


@router.post("/repositories/{repo_id}/test-access")
async def test_access(repo_id: int, p: Admin, request: Request, db: Db, gh: Gh):
    """Mint a token scoped to only this repository and exercise each read permission."""
    repo, inst = await _load_repo(db, repo_id)
    if inst.deleted_at or inst.suspended_at or not repo.accessible:
        raise HTTPException(status.HTTP_409_CONFLICT, "Repository is not accessible to the GitHub App")
    perms = _read_perms(inst)
    full = repo.full_name
    checks: list[dict] = []
    token_info: dict = {}
    try:
        token = await gh.installation_token(inst.installation_id, repository_ids=[repo.github_repo_id], permissions=perms)
        token_info = {"repository_ids": [repo.github_repo_id], "permissions": perms}
    except (GitHubError, GitHubNotConfigured) as e:
        raise _github_error(e) from e

    async def check(name: str, perm: str, fn):
        if perm not in perms:
            checks.append({"name": name, "ok": None, "detail": f"'{perm}' permission not granted to the app"})
            return
        try:
            checks.append({"name": name, "ok": True, "detail": await fn()})
        except GitHubError as e:
            checks.append({"name": name, "ok": False, "detail": str(e)})

    async def meta():
        d = await gh.get(f"/repos/{full}", token)
        repo.default_branch = d.get("default_branch") or repo.default_branch
        repo.private = d.get("private", repo.private)
        return f"default branch '{repo.default_branch}', {'private' if repo.private else 'public'}"

    async def contents():
        d = await gh.get(contents_url(full, ""), token, params={"ref": repo.default_branch})
        return f"{len(d)} entries at repository root"

    async def commit():
        d = await gh.get(f"/repos/{full}/commits/{repo.default_branch}", token)
        return f"head {d['sha'][:12]}: {d['commit']['message'].splitlines()[0][:80]}"

    async def issues():
        d = await gh.get(f"/repos/{full}/issues", token, params={"state": "all", "per_page": 1})
        return "readable" + (f", latest #{d[0]['number']}" if d else ", no issues yet")

    async def pulls():
        d = await gh.get(f"/repos/{full}/pulls", token, params={"state": "all", "per_page": 1})
        return "readable" + (f", latest #{d[0]['number']}" if d else ", no pull requests yet")

    async def check_runs():
        d = await gh.get(f"/repos/{full}/commits/{repo.default_branch}/check-runs", token, params={"per_page": 1})
        return f"{d.get('total_count', 0)} check runs on default branch head"

    await check("Repository metadata", "metadata", meta)
    await check("Read contents", "contents", contents)
    await check("Latest commit", "contents", commit)
    await check("Read issues", "issues", issues)
    await check("Read pull requests", "pull_requests", pulls)
    await check("Read CI checks", "checks", check_runs)

    ok = all(c["ok"] is not False for c in checks) and any(c["ok"] for c in checks)
    repo.last_access_check_at = datetime.now(UTC)
    repo.last_access_ok = ok
    audit(db, "repository.access_test", actor_user_id=p.user.id, target_type="repository", target_id=repo.id,
          ip=auth.client_ip(request), ok=ok, full_name=full)
    await db.commit()
    return {"ok": ok, "token": token_info, "checks": checks}


@router.get("/repositories/{repo_id}/contents")
async def repository_contents(repo_id: int, _: auth.CurrentPrincipal, db: Db, gh: Gh,
                              path: str = "", ref: str | None = None):
    repo, inst = await _load_repo(db, repo_id)
    if not repo.enabled or not repo.accessible or inst.deleted_at or inst.suspended_at:
        raise HTTPException(status.HTTP_409_CONFLICT, "Repository is not enabled in DebugOps")
    try:
        clean_path, clean_ref = safe_repo_path(path), safe_ref(ref) or repo.default_branch
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    try:
        token = await gh.installation_token(inst.installation_id, repository_ids=[repo.github_repo_id],
                                            permissions=_read_perms(inst, ("contents",)) or {"contents": "read"})
        data = await gh.get(contents_url(repo.full_name, clean_path), token, params={"ref": clean_ref})
    except (GitHubError, GitHubNotConfigured) as e:
        raise _github_error(e) from e

    if isinstance(data, list):
        entries = sorted(({"name": d["name"], "path": d["path"], "type": d["type"], "size": d.get("size", 0)} for d in data),
                         key=lambda d: (d["type"] != "dir", d["name"].lower()))
        return {"type": "dir", "path": clean_path, "ref": clean_ref, "entries": entries}

    result = {"type": data.get("type", "file"), "path": clean_path, "ref": clean_ref, "size": data.get("size", 0),
              "sha": data.get("sha"), "content": None, "binary": False, "truncated": False}
    if data.get("type") == "file":
        if data.get("size", 0) > MAX_FILE_BYTES or not data.get("content"):
            result["truncated"] = True
        else:
            raw = base64.b64decode(data["content"])
            try:
                result["content"] = raw.decode("utf-8")
            except UnicodeDecodeError:
                result["binary"] = True
    return result


# ---------------- webhook (public, signature-authenticated) ----------------

@router.post("/github/webhook", include_in_schema=True)
async def github_webhook(request: Request, db: Db):
    s = get_settings()
    if not s.github_webhook_secret:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Webhook secret not configured")
    body = await request.body()
    if not webhooks.verify_signature(s.github_webhook_secret, body, request.headers.get("x-hub-signature-256")):
        log.warning("webhook rejected: bad signature from %s", auth.client_ip(request))
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid signature")
    event = request.headers.get("x-github-event")
    delivery = request.headers.get("x-github-delivery")
    if not event or not delivery:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Missing GitHub event headers")
    try:
        payload = await request.json()
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Body is not JSON") from e
    action = payload.get("action") if isinstance(payload, dict) else None

    # Replay / duplicate guard. A delivery that previously errored may be redelivered and retried.
    inserted = await db.scalar(
        insert(WebhookDelivery)
        .values(delivery_id=delivery, event=event, action=action, status="received", payload=payload,
                installation_id=(payload.get("installation") or {}).get("id"))
        .on_conflict_do_nothing(index_elements=[WebhookDelivery.delivery_id])
        .returning(WebhookDelivery.id)
    )
    if inserted is None:
        existing = (await db.execute(select(WebhookDelivery).where(WebhookDelivery.delivery_id == delivery))).scalar_one()
        if existing.status != "error":
            return {"status": "duplicate"}
        inserted = existing.id
    await db.commit()

    record = await db.get(WebhookDelivery, inserted)
    try:
        record.status = await webhooks.dispatch(db, event, action, payload)
        record.repository_id = await webhooks.resolve_repository_id(db, payload)
        record.error = None
        await db.commit()
    except Exception as e:  # record the failure, return 500 so it shows as failed in GitHub's delivery log
        log.exception("webhook %s (%s) failed", delivery, event)
        await db.rollback()
        record = await db.get(WebhookDelivery, inserted)
        record.status, record.error = "error", f"{type(e).__name__}: {e}"[:2000]
        await db.commit()
        return JSONResponse({"status": "error"}, status_code=500)
    return {"status": record.status}
