"""Developer bug upload, audit record export, post-PR delivery tracking (review -> merge -> CI -> deploy -> health),
centralized error capture."""

import csv
import io
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.github.client import GitHubApp, get_github
from app.main import app
from app.models import ActivityEvent, Bug, Evidence, Job, PullRequest, Repository, Role, WorkflowRun
from app.workflow import delivery
from tests.conftest import logged_in, new_client
from tests.fake_github import FakeGitHub, installation, repo
from tests.test_github import deliver

SECRET = "ghp_" + "Q1w2E3r4T5y6U7i8O9p0A1s2D3f4G5h6J7k8"


@pytest.fixture
def fake():
    f = FakeGitHub()
    f.installations = [installation(perms={"metadata": "read", "contents": "write", "issues": "write",
                                           "pull_requests": "write", "checks": "read", "deployments": "read"})]
    f.repos[100] = [repo(1, "realify/api")]
    gh = GitHubApp(get_settings(), transport=f.transport())
    f.gh = gh
    app.dependency_overrides[get_github] = lambda: gh
    yield f
    app.dependency_overrides.pop(get_github, None)


async def enabled_repo(fake, db, settings=None) -> Repository:
    from app.github.sync import full_sync
    await full_sync(db, fake.gh)
    r = (await db.execute(select(Repository))).scalar_one()
    r.enabled, r.settings = True, settings or {}
    await db.commit()
    return r


# ---------------- developer bug upload ----------------

async def test_developer_reports_bug_with_logs(fake, db):
    r = await enabled_repo(fake, db)
    body = {"title": "Login API returns 500", "description": "POST /login fails for SSO users", "severity": "high",
            "steps_to_reproduce": "1. sign in with SSO", "stack_trace": "Traceback ...\nKeyError: 'email'",
            "affected_files": ["app/auth.py"], "attachments": [{"name": "api.log", "content": f"token={SECRET}\nERROR 500 /login"}],
            "open_github_issue": True, "investigate": True}
    async with logged_in(Role.reviewer) as rev:
        assert (await rev.post(f"/api/v1/repositories/{r.id}/bugs", json=body)).status_code == 403
    async with logged_in(Role.developer) as dev:
        bad = await dev.post(f"/api/v1/repositories/{r.id}/bugs", json={**body, "affected_files": ["../../etc/passwd"]})
        assert bad.status_code == 422
        res = (await dev.post(f"/api/v1/repositories/{r.id}/bugs", json=body)).json()
    bug = await db.get(Bug, res["bug_id"])
    assert bug.source == "manual" and bug.severity == "high" and "Steps to reproduce" in bug.description
    assert bug.affected_files[0]["path"] == "app/auth.py" and bug.github_issue_number == 101
    kinds = {e.kind: e.content for e in (await db.execute(select(Evidence).where(Evidence.bug_id == bug.id))).scalars()}
    assert set(kinds) == {"issue", "stack_trace", "log"}
    assert SECRET not in kinds["log"] and "ERROR 500 /login" in kinds["log"]  # uploaded logs are redacted
    assert res["run_id"]  # investigation started immediately
    assert (await db.execute(select(Job.kind))).scalars().all() == ["workflow.run"]
    run = await db.get(WorkflowRun, res["run_id"])
    assert run.task_title == "Login API returns 500" and run.branch_name == f"debugops/dbg-{run.id:03d}-login-api-returns-500"


# ---------------- delivery lifecycle ----------------

async def make_pr(db, repo_id: int) -> PullRequest:
    bug = Bug(repository_id=repo_id, source="manual", title="Login API returns 500", severity="high", status="pr_open",
              fingerprint="d1", affected_files=[], data={})
    db.add(bug)
    await db.flush()
    run = WorkflowRun(bug_id=bug.id, repository_id=repo_id, status="awaiting_merge", nodes={}, task_title=bug.title,
                      files_changed=["app/auth.py"], branch_name="debugops/dbg-001-login", pr_url="https://github.test/pr/77")
    db.add(run)
    await db.flush()
    pr = PullRequest(run_id=run.id, repository_id=repo_id, number=77, url=run.pr_url, head_branch=run.branch_name,
                     head_sha="headsha", state="open", checks=[])
    db.add(pr)
    await db.commit()
    return pr


async def test_review_merge_ci_deploy_and_healthy(fake, db, monkeypatch):
    r = await enabled_repo(fake, db, {"health_check_url": "https://api.realify.test/health", "health_check_minutes": 1})
    pr = await make_pr(db, r.id)
    base = {"installation": {"id": 100}, "repository": repo(1, "realify/api")}
    async with new_client() as c:
        await deliver(c, "pull_request_review", {**base, "action": "submitted", "pull_request": {"number": 77},
                                                 "review": {"state": "APPROVED", "user": {"login": "lead"}, "submitted_at": "t"}})
        await deliver(c, "pull_request", {**base, "action": "closed", "pull_request": {
            "number": 77, "merged": True, "merged_at": "2026-11-05T10:00:00Z", "merge_commit_sha": "mergesha", "head": {"sha": "headsha"}}})
        await deliver(c, "check_run", {**base, "action": "completed", "check_run": {
            "name": "build", "status": "completed", "conclusion": "success", "head_sha": "mergesha"}})
        await deliver(c, "deployment_status", {**base, "action": "created",
                                               "deployment": {"id": 9, "sha": "mergesha", "environment": "staging"},
                                               "deployment_status": {"state": "success"}})
        assert (await db.scalar(select(Job.id).where(Job.kind == "delivery.health_check"))) is None  # staging: not monitored
        await deliver(c, "deployment_status", {**base, "action": "created",
                                               "deployment": {"id": 10, "sha": "mergesha", "environment": "production"},
                                               "deployment_status": {"state": "success", "environment_url": "https://realify.test"}})
    await db.refresh(pr)
    run = await db.get(WorkflowRun, pr.run_id, populate_existing=True)
    assert pr.review_state == "approved" and pr.state == "merged" and run.status == "succeeded"
    assert pr.merge_commit_sha == "mergesha" and pr.post_merge_ci_status == "success"
    assert [(d["environment"], d["state"]) for d in pr.deployments] == [("staging", "success"), ("production", "success")]
    assert pr.health["status"] == "monitoring"

    async def ok(url, timeout=10.0):
        return {"at": datetime.now(UTC).isoformat(), "ok": True, "status": 200, "ms": 5}
    monkeypatch.setattr(delivery, "probe", ok)
    await delivery.health_check(db, pr.id)
    await db.refresh(pr)
    assert pr.health["status"] == "monitoring" and len(pr.health["checks"]) == 1  # rescheduled
    pr.health = {**pr.health, "until": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()}
    await db.commit()
    await delivery.health_check(db, pr.id)
    await db.refresh(pr)
    assert pr.health["status"] == "healthy"


async def test_failing_health_after_deploy_opens_regression_bug(fake, db, monkeypatch):
    r = await enabled_repo(fake, db, {"health_check_url": "https://api.realify.test/health", "health_failure_threshold": 2,
                                      "investigate_regressions": True})
    pr = await make_pr(db, r.id)
    pr.state, pr.merge_commit_sha = "merged", "mergesha"
    await db.commit()
    await delivery.record_deployment(db, {"deployment": {"id": 11, "sha": "mergesha", "environment": "production"},
                                          "deployment_status": {"state": "success"}})
    await db.commit()

    async def down(url, timeout=10.0):
        return {"at": datetime.now(UTC).isoformat(), "ok": False, "status": 503, "ms": 12}
    monkeypatch.setattr(delivery, "probe", down)
    await delivery.health_check(db, pr.id)
    await delivery.health_check(db, pr.id)
    await db.refresh(pr)
    assert pr.health["status"] == "unhealthy" and pr.health["bug_id"]
    bug = await db.get(Bug, pr.health["bug_id"])
    assert bug.source == "auto_health" and bug.severity == "critical" and "PR #77" in bug.title
    assert bug.affected_files[0]["path"] == "app/auth.py"
    errors = (await db.execute(select(ActivityEvent.message).where(ActivityEvent.level == "error"))).scalars().all()
    assert any("REGRESSION" in m for m in errors)
    assert await db.scalar(select(WorkflowRun.id).where(WorkflowRun.bug_id == bug.id))  # re-entered the debugging workflow
    await delivery.health_check(db, pr.id)  # no duplicate bug once unhealthy
    assert len((await db.execute(select(Bug).where(Bug.source == "auto_health"))).scalars().all()) == 1


async def test_health_probe_real_http():
    def handler(req):
        return httpx.Response(200 if req.url.path == "/ok" else 500)
    import app.workflow.delivery as d
    orig = d.httpx.AsyncClient

    class Client(orig):
        def __init__(self, **kw):
            super().__init__(transport=httpx.MockTransport(handler), **kw)
    d.httpx.AsyncClient = Client
    try:
        assert (await d.probe("https://x.test/ok"))["ok"] is True
        assert (await d.probe("https://x.test/bad"))["status"] == 500
    finally:
        d.httpx.AsyncClient = orig


# ---------------- audit record export ----------------

async def test_runs_csv_export(fake, db):
    r = await enabled_repo(fake, db)
    pr = await make_pr(db, r.id)
    run = await db.get(WorkflowRun, pr.run_id)
    run.task_title, run.base_sha = "=HYPERLINK(\"evil\")", "abc123"
    run.test_summary, run.execution_seconds = {"passed": 120, "failed": 0}, 240
    await db.commit()
    async with logged_in(Role.reviewer) as c:
        text = (await c.get("/api/v1/runs/export.csv")).text
        summary = (await c.get("/api/v1/runs")).json()[0]
    row = next(csv.DictReader(io.StringIO(text)))
    assert row["task_id"] == "DBG-001" and row["base_commit"] == "abc123" and row["branch"] == "debugops/dbg-001-login"
    assert row["tests"].startswith("120 passed") and row["execution_seconds"] == "240" and row["pr_url"].endswith("/77")
    assert row["final_status"] == "awaiting_merge" and row["files_changed"] == "app/auth.py"
    assert row["bug"].startswith("'=")  # spreadsheet formula injection neutralised
    assert summary["task_id"] == "DBG-001" and summary["execution_seconds"] == 240


# ---------------- centralized error capture ----------------

async def test_request_id_and_unhandled_errors_are_captured(db):
    async def boom():
        raise RuntimeError("kaboom")
    app.add_api_route("/api/v1/_test_boom", boom, methods=["GET"])
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as c:
            ok = await c.get("/api/health/live", headers={"X-Request-ID": "trace-abc-123"})
            assert ok.headers["x-request-id"] == "trace-abc-123"
            assert len((await c.get("/api/health/live", headers={"X-Request-ID": "bad id!"})).headers["x-request-id"]) == 32
            r = await c.get("/api/v1/_test_boom")
        assert r.status_code == 500 and "kaboom" not in r.text and r.json()["request_id"]
        ev = (await db.execute(select(ActivityEvent).where(ActivityEvent.level == "error"))).scalar_one()
        assert "500 on GET /api/v1/_test_boom" in ev.message and r.json()["request_id"] in ev.message
        async with logged_in(Role.admin) as admin:
            errs = (await admin.get("/api/v1/activity", params={"level": "error"})).json()
            assert errs[0]["request_id"] == r.json()["request_id"]
            assert (await admin.get("/api/v1/dashboard/summary")).json()["errors_24h"] == 1
    finally:
        app.router.routes[:] = [rt for rt in app.router.routes if getattr(rt, "path", "") != "/api/v1/_test_boom"]


def test_json_log_format():
    import json
    import logging

    from app.observability import JsonFormatter, request_id
    request_id.set("rid-1")
    rec = logging.LogRecord("debugops.x", logging.ERROR, __file__, 1, "failed %s", ("thing",), None)
    out = json.loads(JsonFormatter("worker").format(rec))
    assert out["level"] == "error" and out["service"] == "worker" and out["msg"] == "failed thing" and out["request_id"] == "rid-1"
