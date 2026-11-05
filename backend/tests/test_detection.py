import io
import json
import shutil
import subprocess
import tarfile
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, update

from app.config import get_settings
from app.detection import bugs, jobs, scan
from app.detection.parsers import Finding
from app.github.client import GitHubApp, get_github
from app.main import app
from app.models import Bug, Evidence, Job, Repository, Role, ScanRun, TestResult, TestRun
from app.sandbox.runner import ExecResult
from app.schemas import RepoSettings
from tests.conftest import logged_in, new_client
from tests.fake_github import FakeGitHub, installation, repo
from tests.git_remote import push_files
from tests.test_github import deliver

FAKE_TOKEN = "ghp_" + "Zx9Yw8Vu7Ts6Rq5Po4Nm3Lk2Ji1Hg0FeDcBa"


@pytest.fixture
def fake():
    f = FakeGitHub()
    gh = GitHubApp(get_settings(), transport=f.transport())
    app.dependency_overrides[get_github] = lambda: gh
    f.gh = gh
    yield f
    app.dependency_overrides.pop(get_github, None)


async def setup_repo(fake, db, *, enabled=True, settings=None, perms=None, private=True) -> Repository:
    if perms:
        fake.installations = [installation(perms=perms)]
    fake.repos[100] = [repo(1, "realify/api", private=private)]
    from app.github.sync import full_sync
    await full_sync(db, fake.gh)
    r = (await db.execute(select(Repository))).scalar_one()
    r.enabled = enabled
    r.settings = settings or {}
    await db.commit()
    return r


def finding(fp="fp1", sev="high", evidence="code", source="auto_semgrep", path="app.py") -> Finding:
    return Finding(source=source, title=f"Issue {fp}", description="d", severity=sev, fingerprint=fp,
                   evidence=evidence, evidence_kind="scanner", file_path=path, line_start=1, line_end=1)


# ---------------- test suite detection ----------------

def test_detect_suites():
    cfg = RepoSettings()
    [py] = scan.detect_suites({"requirements.txt", "tests/test_app.py", "app.py"}, None, cfg)
    assert py.report == "junit" and any("-r requirements.txt" in c for c in py.install)
    [node] = scan.detect_suites({"package.json", "package-lock.json"},
                                {"scripts": {"test": "vitest"}, "devDependencies": {"vitest": "1"}}, cfg)
    assert "npm ci --ignore-scripts" in node.install[0] and "junit" in node.test
    [jest] = scan.detect_suites({"package.json"}, {"scripts": {"test": "jest"}, "devDependencies": {"jest": "29"}}, cfg)
    assert jest.report == "jest" and jest.install[0].startswith("npm install")
    assert scan.detect_suites({"package.json"}, {"scripts": {"test": 'echo "Error: no test specified" && exit 1'}}, cfg) == []
    assert scan.detect_suites({"README.md"}, None, cfg) == []
    [custom] = scan.detect_suites(set(), None, RepoSettings(test_command="make test", install_command="make deps"))
    assert (custom.install, custom.test) == (["make deps"], "make test")
    [scripts_on] = scan.detect_suites({"package.json"}, {"scripts": {"test": "x"}}, RepoSettings(allow_install_scripts=True))
    assert "--ignore-scripts" not in scripts_on.install[0]


# ---------------- dedupe ----------------

async def test_record_findings_dedupes_and_reopens(fake, db):
    r = await setup_repo(fake, db)
    s1, s2, s3 = (ScanRun(repository_id=r.id, trigger="manual", status="succeeded") for _ in range(3))
    db.add_all([s1, s2, s3])
    await db.flush()

    stats = await bugs.record_findings(db, r, s1, [finding("a"), finding("a", path="other.py"), finding("b")])
    await db.commit()
    assert stats == {"new": 2, "seen_again": 0, "reopened": 0}
    a = (await db.execute(select(Bug).where(Bug.fingerprint == "a"))).scalar_one()
    assert [loc["path"] for loc in a.affected_files] == ["app.py", "other.py"]  # merged within the scan

    a.status = "resolved"
    await db.commit()
    stats = await bugs.record_findings(db, r, s2, [finding("a"), finding("b", evidence="changed")])
    await db.commit()
    assert stats == {"new": 0, "seen_again": 2, "reopened": 1}
    await db.refresh(a)
    assert a.status == "new" and a.occurrences == 2 and a.last_seen_scan_id == s2.id
    counts = dict((await db.execute(select(Bug.fingerprint, func.count(Evidence.id)).join(Evidence).group_by(Bug.fingerprint))).all())
    assert counts == {"a": 1, "b": 2}  # unchanged evidence not duplicated; changed evidence kept
    assert await db.scalar(select(func.count()).select_from(Bug)) == 2


# ---------------- issue ingestion ----------------

def issue(n=5, labels=("bug",), state="open", body="It crashes", **kw):
    return {"number": n, "title": f"Crash {n}", "body": body, "state": state, "html_url": f"https://github.test/i/{n}",
            "labels": [{"name": lbl} for lbl in labels], "user": {"login": "dev"}, **kw}


async def test_issue_webhooks(fake, db):
    await setup_repo(fake, db)
    base = {"installation": {"id": 100}, "repository": repo(1, "realify/api")}
    async with new_client() as c:
        assert (await deliver(c, "issues", {**base, "action": "opened", "issue": issue(1, labels=("question",))})).json()["status"] == "ignored"
        assert (await deliver(c, "issues", {**base, "action": "opened", "issue": issue(2, labels=("critical", "bug"),
                                                                                         body=f"token={FAKE_TOKEN}")})).json()["status"] == "processed"
        assert (await deliver(c, "issues", {**base, "action": "opened", "issue": issue(3, pull_request={})})).json()["status"] == "ignored"
        assert (await deliver(c, "issues", {**base, "action": "opened", "issue": issue(4, body="<!-- debugops:fp=x -->")})).json()["status"] == "ignored"
        bug = (await db.execute(select(Bug))).scalar_one()
        assert bug.severity == "critical" and bug.github_issue_number == 2 and FAKE_TOKEN not in bug.description

        # Label removed later, but the issue is already tracked: edits and closing still sync.
        await deliver(c, "issues", {**base, "action": "edited", "issue": issue(2, labels=(), body="More detail")})
        await deliver(c, "issues", {**base, "action": "closed", "issue": issue(2, labels=(), state="closed", body="More detail")})
        await db.refresh(bug)
        assert bug.description == "More detail" and bug.status == "closed"
        await deliver(c, "issues", {**base, "action": "reopened", "issue": issue(2, labels=())})
        await db.refresh(bug)
        assert bug.status == "new"


async def test_import_issues_job(fake, db, monkeypatch):
    monkeypatch.setattr(jobs, "get_github", lambda: fake.gh)
    r = await setup_repo(fake, db, settings={"ingest_labels": []})
    fake.issues = [issue(10, labels=()), issue(11, pull_request={}), issue(12, labels=("bug",))]
    await jobs.import_issues(db, Job(kind="github.import_issues", payload={"repository_id": r.id}))
    assert sorted((await db.execute(select(Bug.github_issue_number))).scalars().all()) == [10, 12]


async def test_push_triggers_scan_only_when_configured(fake, db):
    r = await setup_repo(fake, db)
    base = {"installation": {"id": 100}, "repository": repo(1, "realify/api"), "ref": "refs/heads/main"}
    async with new_client() as c:
        assert (await deliver(c, "push", base)).json()["status"] == "ignored"  # scan_on_push off
        r.settings = {"scan_on_push": True}
        await db.commit()
        assert (await deliver(c, "push", {**base, "ref": "refs/heads/feature"})).json()["status"] == "ignored"
        assert (await deliver(c, "push", base)).json()["status"] == "processed"
        assert (await deliver(c, "push", base)).json()["status"] == "processed"  # second push: scan already queued
    assert await db.scalar(select(func.count()).select_from(ScanRun)) == 1
    assert (await db.execute(select(ScanRun.trigger))).scalar_one() == "push"


# ---------------- scan API / scheduler ----------------

async def test_scan_api_rbac_and_single_active_scan(fake, db):
    r = await setup_repo(fake, db)
    async with logged_in(Role.reviewer) as rev:
        assert (await rev.post(f"/api/v1/repositories/{r.id}/scans")).status_code == 403
        assert (await rev.get("/api/v1/scans")).status_code == 200
    async with logged_in(Role.developer) as dev:
        first = await dev.post(f"/api/v1/repositories/{r.id}/scans")
        assert first.status_code == 201 and first.json()["status"] == "queued"
        assert (await dev.post(f"/api/v1/repositories/{r.id}/scans")).status_code == 409
        assert (await dev.patch(f"/api/v1/repositories/{r.id}", json={"settings": {}})).status_code == 403  # admin only
    assert (await db.execute(select(Job.kind))).scalar_one() == "scan.run"


async def test_repo_settings_validation(fake, db):
    r = await setup_repo(fake, db)
    async with logged_in(Role.admin) as admin:
        bad = await admin.patch(f"/api/v1/repositories/{r.id}", json={"settings": {"scan_interval_hours": -1}})
        assert bad.status_code == 422
        ok = await admin.patch(f"/api/v1/repositories/{r.id}", json={"settings": {"test_command": "make test", "auto_create_issues": True}})
        assert ok.status_code == 200 and ok.json()["settings"]["test_command"] == "make test"
        assert ok.json()["settings"]["run_semgrep"] is True  # defaults filled in


async def test_scheduler(fake, db):
    await setup_repo(fake, db, settings={"scan_interval_hours": 24})
    assert await jobs.enqueue_scheduled_scans(db) == 1
    assert await jobs.enqueue_scheduled_scans(db) == 0  # one already queued
    await db.execute(update(ScanRun).values(status="succeeded", created_at=datetime.now(UTC) - timedelta(hours=1)))
    await db.commit()
    assert await jobs.enqueue_scheduled_scans(db) == 0  # not due yet
    await db.execute(update(ScanRun).values(created_at=datetime.now(UTC) - timedelta(hours=25)))
    await db.commit()
    assert await jobs.enqueue_scheduled_scans(db) == 1


# ---------------- scan pipeline (fake sandbox) ----------------

def gz_repo(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for name, content in {"realify-api-abc/": None, **{f"realify-api-abc/{k}": v for k, v in files.items()}}.items():
            info = tarfile.TarInfo(name.rstrip("/"))
            if content is None:
                info.type = tarfile.DIRTYPE
                t.addfile(info)
            else:
                data = content.encode()
                info.size = len(data)
                t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


SEMGREP_OUT = json.dumps({"results": [{"check_id": "python.lang.security.audit.eval-detected.eval-detected", "path": "app.py",
                                       "start": {"line": 1}, "end": {"line": 1},
                                       "extra": {"message": "eval", "severity": "WARNING", "metadata": {}}}]})
JUNIT_OUT = ('<testsuite><testcase classname="tests.test_app" name="test_add"><failure message="assert -1 == 5"/></testcase>'
             '<testcase classname="tests.test_app" name="test_ok"/></testsuite>')


class FakeSandbox:
    instances: list["FakeSandbox"] = []
    files: dict[str, str] = {}
    fail_tool: str | None = None

    def __init__(self, settings, client=None, label=None):
        self.commands, self.destroyed = [], False
        FakeSandbox.instances.append(self)

    def start(self, source):
        self.source = source

    def exec(self, cmd, timeout, workdir=None, env=None):
        self.commands.append(cmd)
        if cmd[0] == self.fail_tool:
            return ExecResult(2, "tool crashed", False, 5)
        if cmd[0] == "osv-scanner":
            return ExecResult(128, "no package sources found", False, 5)
        return ExecResult(1 if "pytest" in " ".join(cmd) else 0, "output", False, 10)

    def read_file(self, path, max_bytes=0):
        name = path.rsplit("/", 1)[-1]
        return self.files[name].encode() if name in self.files else None

    def destroy(self):
        self.destroyed = True


@pytest.fixture
def fake_sandbox(monkeypatch):
    FakeSandbox.instances, FakeSandbox.fail_tool = [], None
    FakeSandbox.files = {"semgrep.json": SEMGREP_OUT, "gitleaks.json": "[]", "junit-python.xml": JUNIT_OUT}
    monkeypatch.setattr(scan, "Sandbox", FakeSandbox)
    return FakeSandbox


async def test_scan_pipeline_records_findings_and_tests(fake, db, fake_sandbox):
    r = await setup_repo(fake, db)
    head_sha = push_files("realify/api", {"app.py": "eval(input())\n", "requirements.txt": "", "tests/test_app.py": "def test_add(): ..."})
    s = await scan.request_scan(db, r, "manual")
    await scan.execute(db, s.id, fake.gh)

    await db.refresh(s)
    assert s.status == "succeeded", (s.error, s.steps)
    assert s.commit_sha == head_sha
    assert {k: v["status"] for k, v in s.steps.items()} == {
        "fetch": "ok", "sandbox": "ok", "semgrep": "ok", "gitleaks": "ok", "osv": "ok", "tests": "ok", "record": "ok"}
    assert s.summary["tests"] == {"passed": 1, "failed": 1, "errors": 0, "skipped": 0} and s.summary["new"] == 2
    titles = sorted((await db.execute(select(Bug.title))).scalars().all())
    assert titles == ["Failing test: tests.test_app::test_add", "eval detected in app.py"]
    ev = (await db.execute(select(Evidence).join(Bug).where(Bug.source == "auto_semgrep"))).scalar_one()
    assert ev.content == "eval(input())" and ev.commit_sha == head_sha  # snippet read from the real source
    assert await db.scalar(select(func.count()).select_from(TestResult)) == 2
    sb = fake_sandbox.instances[0]
    assert sb.destroyed and "app.py" in sb.source.files
    assert any("pip install" in " ".join(c) for c in sb.commands)

    # A second scan of unchanged code finds the same bugs: no new records.
    s2 = await scan.request_scan(db, r, "manual")
    await scan.execute(db, s2.id, fake.gh)
    await db.refresh(s2)
    assert s2.summary["new"] == 0 and s2.summary["seen_again"] == 2


async def test_scan_tool_failure_is_partial_not_hidden(fake, db, fake_sandbox):
    r = await setup_repo(fake, db, settings={"run_tests": False})
    push_files("realify/api", {"app.py": "eval(input())\n"})
    fake_sandbox.fail_tool = "gitleaks"
    s = await scan.request_scan(db, r, "manual")
    await scan.execute(db, s.id, fake.gh)
    await db.refresh(s)
    assert s.status == "partial" and s.steps["gitleaks"]["status"] == "failed" and "exit 2" in s.steps["gitleaks"]["detail"]
    assert s.steps["semgrep"]["status"] == "ok" and s.steps["tests"]["status"] == "skipped"
    assert s.summary["failed_steps"] == ["gitleaks"]


async def test_scan_fails_cleanly_when_github_fails(fake, db, fake_sandbox):
    r = await setup_repo(fake, db)
    r.full_name = "realify/missing"  # no such repository on the remote: the fetch fails
    await db.commit()
    s = await scan.request_scan(db, r, "manual")
    await scan.execute(db, s.id, fake.gh)
    await db.refresh(s)
    assert s.status == "failed" and "git fetch failed" in s.error and s.finished_at
    assert s.steps["fetch"]["status"] == "failed"
    assert fake_sandbox.instances == []  # never started a sandbox


async def test_auto_issue_creation_queued_by_threshold(fake, db, fake_sandbox):
    r = await setup_repo(fake, db, settings={"auto_create_issues": True, "auto_issue_min_severity": "high", "run_tests": False})
    push_files("realify/api", {"app.py": "eval(input())\n"})  # semgrep WARNING -> medium: below threshold
    fake_sandbox.files["gitleaks.json"] = json.dumps([{"RuleID": "github-pat", "File": "app.py", "StartLine": 1,
                                                       "Secret": FAKE_TOKEN, "Match": FAKE_TOKEN}])
    s = await scan.request_scan(db, r, "manual")
    await scan.execute(db, s.id, fake.gh)
    payloads = (await db.execute(select(Job.payload).where(Job.kind == "github.create_issue"))).scalars().all()
    critical = (await db.execute(select(Bug.id).where(Bug.severity == "critical"))).scalar_one()
    assert payloads == [{"bug_id": critical}]


# ---------------- GitHub issue creation ----------------

async def test_create_github_issue_guards_and_body(fake, db):
    r = await setup_repo(fake, db, perms={"metadata": "read", "contents": "read", "issues": "write"})
    s = ScanRun(repository_id=r.id, trigger="manual", status="succeeded", commit_sha="abc123")
    db.add(s)
    await db.flush()
    await bugs.record_findings(db, r, s, [finding("sec", sev="critical", source="auto_gitleaks", evidence="TOKEN = ghp_****")])
    await db.commit()
    bug = (await db.execute(select(Bug))).scalar_one()

    async with logged_in(Role.developer) as dev:
        res = await dev.post(f"/api/v1/bugs/{bug.id}/github-issue")
        assert res.status_code == 200 and res.json()["github_issue_number"] == 101
        assert (await dev.post(f"/api/v1/bugs/{bug.id}/github-issue")).json()["github_issue_number"] == 101  # idempotent
    [created] = fake.created_issues
    assert f"<!-- debugops:fp={bug.fingerprint} -->" in created["body"]
    assert "blob/abc123/app.py#L1" in created["body"] and "ghp_****" in created["body"]


async def test_create_github_issue_refusals(fake, db):
    r = await setup_repo(fake, db, private=False, perms={"metadata": "read", "issues": "write"})
    s = ScanRun(repository_id=r.id, trigger="manual", status="succeeded")
    db.add(s)
    await db.flush()
    await bugs.record_findings(db, r, s, [finding("sec", source="auto_osv"), finding("t", source="auto_test"),
                                          finding("t2", source="auto_test")])
    await db.commit()
    sec, test_bug, other = (await db.execute(select(Bug).order_by(Bug.id))).scalars().all()
    async with logged_in(Role.developer) as dev:
        res = await dev.post(f"/api/v1/bugs/{sec.id}/github-issue")
        assert res.status_code == 409 and "public" in res.json()["detail"]
        assert (await dev.post(f"/api/v1/bugs/{test_bug.id}/github-issue")).status_code == 200  # non-security is fine
    fake.installations = [installation(perms={"metadata": "read", "issues": "read"})]
    from app.github.sync import full_sync
    await full_sync(db, fake.gh)
    await db.commit()
    with pytest.raises(bugs.IssueCreationRefused, match="issues: write"):
        await bugs.create_github_issue(db, fake.gh, other, actor_user_id=None, web_url="https://github.com")


async def test_existing_marker_issue_is_linked_not_duplicated(fake, db):
    r = await setup_repo(fake, db, perms={"metadata": "read", "issues": "write"})
    s = ScanRun(repository_id=r.id, trigger="manual", status="succeeded")
    db.add(s)
    await db.flush()
    await bugs.record_findings(db, r, s, [finding("x", source="auto_test")])
    await db.commit()
    bug = (await db.execute(select(Bug))).scalar_one()
    fake.search_items = [{"number": 55, "html_url": "https://github.test/i/55"}]
    await bugs.create_github_issue(db, fake.gh, bug, actor_user_id=None, web_url="https://github.com")
    assert bug.github_issue_number == 55 and fake.created_issues == []


# ---------------- bugs API ----------------

async def test_bugs_api(fake, db):
    r = await setup_repo(fake, db)
    s1 = ScanRun(repository_id=r.id, trigger="manual", status="succeeded")
    db.add(s1)
    await db.flush()
    await bugs.record_findings(db, r, s1, [
        Finding("auto_test", "Failing test: tests.test_cart::test_checkout_total", "", "medium", "f1", "e", "test_output"),
        Finding("auto_test", "Failing test: tests.test_cart::test_checkout_totals", "", "medium", "f2", "e", "test_output"),
        Finding("auto_semgrep", "sql injection in db.py", "", "high", "f3", "e", "scanner")])
    s2 = ScanRun(repository_id=r.id, trigger="manual", status="succeeded")
    db.add(s2)
    await db.flush()
    await bugs.record_findings(db, r, s2, [Finding("auto_semgrep", "sql injection in db.py", "", "high", "f3", "e", "scanner")])
    await db.commit()

    async with logged_in(Role.developer) as dev:
        all_bugs = (await dev.get("/api/v1/bugs")).json()
        assert len(all_bugs) == 3
        stale = {b["title"]: b["stale"] for b in all_bugs}
        assert stale["sql injection in db.py"] is False and stale["Failing test: tests.test_cart::test_checkout_total"] is True
        assert len((await dev.get("/api/v1/bugs", params={"severity": "high"})).json()) == 1
        assert len((await dev.get("/api/v1/bugs", params={"q": "checkout"})).json()) == 2

        b1 = next(b for b in all_bugs if b["title"].endswith("test_checkout_total"))
        b2 = next(b for b in all_bugs if b["title"].endswith("test_checkout_totals"))
        detail = (await dev.get(f"/api/v1/bugs/{b1['id']}")).json()
        assert detail["similar"][0]["id"] == b2["id"]  # pg_trgm flags the near-identical title

        assert (await dev.patch(f"/api/v1/bugs/{b1['id']}", json={"status": "triaged", "severity": "high"})).json()["severity"] == "high"
        assert (await dev.patch(f"/api/v1/bugs/{b1['id']}", json={"status": "bogus"})).status_code == 422
        assert (await dev.post(f"/api/v1/bugs/{b1['id']}/mark-duplicate", json={"of": b1["id"]})).status_code == 422
        dup = await dev.post(f"/api/v1/bugs/{b2['id']}/mark-duplicate", json={"of": b1["id"]})
        assert dup.json()["status"] == "duplicate" and dup.json()["duplicate_of"] == b1["id"]
        assert (await dev.post(f"/api/v1/bugs/{b1['id']}/mark-duplicate", json={"of": b2["id"]})).status_code == 422  # no cycles
        assert len((await dev.get("/api/v1/bugs", params={"open_only": True})).json()) == 2

        summary = (await dev.get("/api/v1/dashboard/summary")).json()
        assert summary["open_bugs_by_severity"]["high"] == 2

    async with logged_in(Role.reviewer) as rev:
        assert (await rev.patch(f"/api/v1/bugs/{b1['id']}", json={"status": "resolved"})).status_code == 403


# ---------------- real sandbox integration ----------------

def _sandbox_image_available() -> bool:
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "image", "inspect", get_settings().sandbox_image], capture_output=True).returncode == 0


@pytest.mark.skipif(not _sandbox_image_available(), reason="Docker or the debugops-sandbox image is not available")
async def test_real_sandbox_end_to_end(fake, db, monkeypatch):
    """Full engine against real Docker: semgrep + gitleaks + tests inside the actual sandbox image, no network."""
    monkeypatch.setattr(get_settings(), "sandbox_network", "none")
    r = await setup_repo(fake, db, settings={
        "run_osv": False,  # needs network; covered by the compose e2e run
        "test_command": "python -m unittest discover -s tests -p 'test_*.py'"})
    push_files("realify/api", {
        "app.py": "import subprocess\n\n\ndef run(cmd):\n    return subprocess.call(cmd, shell=True)\n\n\ndef add(a, b):\n    return a - b\n",
        "config.py": f'TOKEN = "{FAKE_TOKEN}"\n',
        "tests/__init__.py": "",
        "tests/test_app.py": "import unittest\nfrom app import add\n\n\nclass T(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n",
    })
    s = await scan.request_scan(db, r, "manual")
    await scan.execute(db, s.id, fake.gh)
    await db.refresh(s)
    assert s.status == "succeeded", (s.error, s.steps)
    sources = sorted((await db.execute(select(Bug.source))).scalars().all())
    assert sources == ["auto_gitleaks", "auto_semgrep", "auto_test"], s.steps
    secret_bug = (await db.execute(select(Bug).where(Bug.source == "auto_gitleaks"))).scalar_one()
    all_text = json.dumps([secret_bug.title, secret_bug.description, secret_bug.data]) + \
        " ".join((await db.execute(select(Evidence.content))).scalars().all()) + \
        " ".join((await db.execute(select(TestRun.log_tail))).scalars().all())
    assert FAKE_TOKEN not in all_text  # plaintext secret never persisted anywhere
    run = (await db.execute(select(TestRun))).scalar_one()
    assert run.exit_code == 1 and run.result_format == "exit_code" and "AssertionError" in run.log_tail
    leftovers = subprocess.run(["docker", "ps", "-aq", "--filter", "label=debugops.sandbox"], capture_output=True, text=True).stdout.strip()
    assert leftovers == ""  # sandbox removed
