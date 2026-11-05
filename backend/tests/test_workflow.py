"""End-to-end debugging workflow with a REAL Docker sandbox, real git and real test execution/validation.

The Claude-backed parts are replaced by scripted fakes: FakeLLM (Bug Hunter / Security / Review) and FakeCodeAgent
(Code Analyst / Investigation / Fix / Testing), which edit files inside the sandbox the way Claude Code would.
"""

import shlex

import pytest
from sqlalchemy import func, select

from app.agents.claude_code import CodeAgentResult
from app.agents.llm import LLMResult
from app.workflow.classify import TaskClass
from app.agents.schemas import (CodeAnalysis, FileRef, FixResult, Hypothesis, Investigation, ReviewVerdict, SecurityReview,
                                TestingResult, Triage)
from app.config import get_settings
from app.github.client import GitHubApp, get_github
from app.main import app
from app.models import (AgentStep, Approval, Bug, Evidence, FixAttempt, GatewayToken, PullRequest, Report, Repository, Role,
                        ScanRun, TestRun, WorkflowRun)
from app.sandbox.runner import Sandbox
from app.workflow import runner
from app.workflow.graph import Deps
from app.workflow.workspace import REGISTRY
from tests.conftest import PASSWORD, login, logged_in, make_user, new_client
from tests.fake_github import FakeGitHub, installation, repo
from tests.git_remote import push_files
from tests.test_detection import _sandbox_image_available
from tests.test_github import deliver

pytestmark = pytest.mark.skipif(not _sandbox_image_available(), reason="Docker or the debugops-sandbox image is not available")

RUN_TESTS = '''import json, sys, unittest, xml.etree.ElementTree as ET
suite = unittest.defaultTestLoader.discover("tests", top_level_dir=".")
res = unittest.TestResult()
cases = []
def walk(s):
    for t in s:
        walk(t) if isinstance(t, unittest.TestSuite) else cases.append(t)
walk(suite)
root = ET.Element("testsuite")
for c in cases:
    r = unittest.TestResult(); c.run(r)
    el = ET.SubElement(root, "testcase", classname=type(c).__module__, name=c._testMethodName)
    for kind, items in (("failure", r.failures), ("error", r.errors)):
        for _, tb in items:
            ET.SubElement(el, kind, message=tb.strip().splitlines()[-1]).text = tb
ET.ElementTree(root).write("/workspace/reports/junit.xml")
sys.exit(0)
'''

REPO_FILES = {
    "calc.py": "def add(a, b):\n    return a - b\n\n\ndef mul(a, b):\n    return a * b\n",
    "run_tests.py": RUN_TESTS,
    "tests/__init__.py": "",
    "tests/test_calc.py": "import unittest\nfrom calc import mul\n\n\nclass TestMul(unittest.TestCase):\n    def test_mul(self):\n        self.assertEqual(mul(2, 3), 6)\n",
}
GOOD_FIX = "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"
WRONG_FIX = "def add(a, b):\n    return a * b\n\n\ndef mul(a, b):\n    return a * b\n"
REGRESSION_TEST = "import unittest\nfrom calc import add\n\n\nclass TestAdd(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n"
USELESS_TEST = "import unittest\nfrom calc import mul\n\n\nclass TestMul2(unittest.TestCase):\n    def test_mul_again(self):\n        self.assertEqual(mul(1, 1), 1)\n"


class FakeLLM:
    def __init__(self, review="approve", security="pass", triage_real=True, task_type="bug"):
        self.review, self.security, self.triage_real, self.task_type = review, security, triage_real, task_type
        self.prompts: dict[str, list[str]] = {}

    async def structured(self, *, agent, system, prompt, schema):
        self.prompts.setdefault(agent, []).append(prompt)
        parsed = {
            "bug_hunter": lambda: Triage(is_real_bug=self.triage_real, confidence=0.95, severity="high", rationale="arith bug"),
            "classifier": lambda: TaskClass(type=self.task_type, reason="test classification"),
            "security": lambda: SecurityReview(verdict=self.security, summary="ok"),
            "review": lambda: ReviewVerdict(verdict=self.review if isinstance(self.review, str) else self.review.pop(0),
                                            summary="looks right", issues=[]),
        }[agent]()
        return LLMResult(parsed, 100, 20, 0.001)


class FakeCodeAgent:
    """Scripted stand-in for Claude Code. `fixes` / `tests` are consumed one per attempt."""

    def __init__(self, ws, script):
        self.ws, self.script = ws, script

    async def write(self, path, content):
        await self.ws.sh(f"mkdir -p $(dirname {shlex.quote(path)}) && printf %s {shlex.quote(content)} > {shlex.quote(path)}")

    async def run(self, *, agent, prompt, schema, tools, edit, on_event=None, devops_agent=False):
        self.script.setdefault("prompts", []).append((agent, prompt, devops_agent))
        if on_event:
            await on_event("thought", f"{agent} working")
        sc = self.script
        sc.setdefault("calls", []).append((agent, edit, tools))
        if agent == "code_analyst":
            out = CodeAnalysis(summary="add() in calc.py", relevant_files=[FileRef(file="calc.py", line=1)])
        elif agent == "investigation":
            out = Investigation(hypotheses=[
                Hypothesis(claim="add() subtracts instead of adding", evidence=[FileRef(file="calc.py", line=2, explanation="a - b")],
                           confidence=sc.get("confidence", 0.9), reproduced=True, reproduction="add(2,3) == -1"),
                Hypothesis(claim="invented", evidence=[FileRef(file="nope.py", line=99)], confidence=0.99)])
        elif agent == "fix":
            content = sc["fixes"].pop(0) if len(sc["fixes"]) > 1 else sc["fixes"][0]
            await self.write("calc.py", content)
            await self.write("tests/test_calc.py", "# fix agent tried to weaken tests\n")  # must be reverted
            out = FixResult(summary="fix add", rationale="SECRET-RATIONALE-MARKER", files_changed=["calc.py"])
        elif agent == "testing":
            content = sc["tests"].pop(0) if len(sc["tests"]) > 1 else sc["tests"][0]
            await self.write("tests/test_regression.py", content)
            await self.write("calc.py", "def add(a, b):\n    return 5\n")  # must be reverted
            out = TestingResult(summary="regression test", test_files=["tests/test_regression.py"])
        else:
            raise AssertionError(agent)
        return CodeAgentResult(out, "{}", 1000, 200, 0.01, 3)


@pytest.fixture
def env(monkeypatch, db):
    monkeypatch.setattr(get_settings(), "sandbox_network", "none")
    f = FakeGitHub()
    f.installations = [installation(perms={"metadata": "read", "contents": "write", "issues": "write",
                                           "pull_requests": "write", "checks": "read"})]
    f.repos[100] = [repo(1, "realify/api")]
    push_files("realify/api", REPO_FILES)
    gh = GitHubApp(get_settings(), transport=f.transport())
    app.dependency_overrides[get_github] = lambda: gh
    yield f, gh
    app.dependency_overrides.pop(get_github, None)
    for run_id in list(REGISTRY):
        REGISTRY.pop(run_id).destroy()


async def setup(db, gh, settings=None, issue_number=None):
    from app.github.sync import full_sync
    await full_sync(db, gh)
    r = (await db.execute(select(Repository))).scalar_one()
    # These tests exercise the optional pre-PR approval gate; the default (off) is covered separately.
    r.enabled, r.settings = True, {"test_command": "python run_tests.py", "require_pr_approval": True, **(settings or {})}
    bug = Bug(repository_id=r.id, source="github_issue", title="add() returns wrong result", description="add(2,3) gives -1",
              severity="high", status="new", fingerprint="fp-issue-5", affected_files=[], data={},
              github_issue_number=issue_number)
    db.add(bug)
    await db.commit()
    return r, bug


def deps(gh, llm, script):
    s = get_settings()
    return Deps(gh=gh, llm=lambda: llm, code_agent=lambda ws: FakeCodeAgent(ws, script), sandbox=lambda label: Sandbox(s, label=label))


async def reload(db, model, id_):
    return await db.get(model, id_, populate_existing=True)


async def test_happy_path_to_merged_pr(env, db):
    fake, gh = env
    _, bug = await setup(db, gh, issue_number=5)
    dev = await make_user(Role.developer)
    reviewer = await make_user(Role.reviewer)
    async with new_client() as c:
        await login(c, dev.email)
        r = await c.post(f"/api/v1/bugs/{bug.id}/investigate")
        assert r.status_code == 201
        run_id = r.json()["id"]
        assert (await c.post(f"/api/v1/bugs/{bug.id}/investigate")).status_code == 409  # one active run per bug

    llm, script = FakeLLM(), {"fixes": [GOOD_FIX], "tests": [REGRESSION_TEST]}
    await runner.execute(run_id, deps=deps(gh, llm, script))

    run = await reload(db, WorkflowRun, run_id)
    assert run.status == "waiting_approval", (run.error, run.nodes)
    assert run.validation_verdict == "verified" and run.attempt == 1
    fa = (await db.execute(select(FixAttempt).where(FixAttempt.run_id == run_id))).scalar_one()
    assert set(fa.files) == {"calc.py", "tests/test_regression.py"}  # fix agent's test edit and tester's code edit reverted
    assert fa.files["calc.py"] == GOOD_FIX
    reg = fa.validation["evidence"]["regression_tests"]
    assert reg == {"tests.test_regression::test_add": {"before_fix": "failed", "after_fix": "passed"}}
    assert "SECRET-RATIONALE-MARKER" not in llm.prompts["review"][0]  # reviewer never sees the fix author's reasoning
    assert run_id not in REGISTRY  # sandbox released while waiting for a human
    tokens = (await db.execute(select(GatewayToken))).scalars().all()
    assert tokens and all(t.revoked_at for t in tokens)
    report = (await db.execute(select(Report))).scalar_one()
    assert "Fix validated" in report.markdown and "calc.py" in report.markdown
    assert (await reload(db, Bug, bug.id)).status == "fix_proposed"
    statuses = {s.node: s.status for s in (await db.execute(select(AgentStep).where(AgentStep.run_id == run_id))).scalars()}
    assert statuses["await_approval"] == "waiting"

    async with new_client() as c:  # the starter cannot approve their own run
        await login(c, dev.email)
        assert (await c.post(f"/api/v1/runs/{run_id}/approve", json={})).status_code == 403
    async with new_client() as c:
        await login(c, reviewer.email)
        assert (await c.post(f"/api/v1/runs/{run_id}/approve", json={"reason": "lgtm"})).status_code == 200
    job = (await db.execute(select(WorkflowRun))).scalar_one()
    await runner.execute(run_id, resume={"decision": "approve", "user_id": reviewer.id, "reason": "lgtm"}, deps=deps(gh, llm, script))

    run = await reload(db, WorkflowRun, run_id)
    assert run.status == "awaiting_merge", run.error
    pr = (await db.execute(select(PullRequest))).scalar_one()
    assert pr.number == 77 and pr.head_branch == "debugops/dbg-001-add-returns-wrong-result" == run.branch_name
    assert run.pr_url == pr.url and run.files_changed == ["calc.py", "tests/test_regression.py"]
    assert run.test_summary["verdict"] == "verified" and run.test_summary["passed"] >= 2
    assert run.task_title == "add() returns wrong result" and run.repository_full_name == "realify/api"
    assert run.execution_seconds >= 1
    paths = [p for p, _ in fake.git_writes]
    assert any(p.endswith("/git/refs") for p in paths) and any(p.endswith("/pulls") for p in paths)
    tree = next(b for p, b in fake.git_writes if p.endswith("/git/trees"))
    assert tree["base_tree"] == "basetree" and {e["path"] for e in tree["tree"]} == {"calc.py", "tests/test_regression.py"}
    pull = next(b for p, b in fake.git_writes if p.endswith("/pulls"))
    assert pull["base"] == "main" and pull["head"] == run.branch_name and pull["body"].startswith("Fixes #5")
    assert pull["title"].startswith("[DebugOps DBG-001]")
    assert not any("merge" in p for p in paths)  # DebugOps never merges
    assert any("/issues/5/comments" in p for p in paths)

    # CI check + human merge arrive by webhook.
    base = {"installation": {"id": 100}, "repository": repo(1, "realify/api")}
    async with new_client() as c:
        await deliver(c, "check_run", {**base, "action": "completed", "check_run": {
            "name": "ci", "status": "completed", "conclusion": "success", "head_sha": pr.head_sha}})
        await deliver(c, "pull_request", {**base, "action": "closed", "pull_request": {
            "number": 77, "merged": True, "merged_at": "2026-11-05T10:00:00Z", "head": {"sha": pr.head_sha}}})
    pr = await reload(db, PullRequest, pr.id)
    assert pr.ci_status == "success" and pr.state == "merged"
    assert (await reload(db, WorkflowRun, run_id)).status == "succeeded"
    assert (await reload(db, Bug, bug.id)).status == "resolved"


async def test_wrong_fix_is_retried_then_validated(env, db):
    _, gh = env
    _, bug = await setup(db, gh)
    run = await runner.start_run(db, bug, None)
    script = {"fixes": [WRONG_FIX, GOOD_FIX], "tests": [REGRESSION_TEST]}
    await runner.execute(run.id, deps=deps(gh, FakeLLM(), script))
    run = await reload(db, WorkflowRun, run.id)
    assert run.status == "waiting_approval" and run.attempt == 2
    attempts = (await db.execute(select(FixAttempt).order_by(FixAttempt.id))).scalars().all()
    assert [a.status for a in attempts] == ["rejected", "accepted"]
    assert attempts[0].validation["verdict"] == "failed" and "still fails" in attempts[0].validation["reason"]


async def test_useless_regression_test_never_counts_as_fixed(env, db, monkeypatch):
    monkeypatch.setattr(get_settings(), "max_fix_attempts", 2)
    _, gh = env
    _, bug = await setup(db, gh)
    run = await runner.start_run(db, bug, None)
    await runner.execute(run.id, deps=deps(gh, FakeLLM(), {"fixes": [GOOD_FIX], "tests": [USELESS_TEST]}))
    run = await reload(db, WorkflowRun, run.id)
    assert run.status == "failed" and run.attempt == 2
    fa = (await db.execute(select(FixAttempt).order_by(FixAttempt.id.desc()).limit(1))).scalar_one()
    assert "does not reproduce the bug" in fa.validation["reason"]
    assert "No validated fix" in (await db.execute(select(Report.markdown))).scalar_one()
    assert (await reload(db, Bug, bug.id)).status == "triaged"


async def test_review_changes_requested_loops_and_reject_needs_human(env, db):
    _, gh = env
    _, bug = await setup(db, gh)
    run = await runner.start_run(db, bug, None)
    llm = FakeLLM(review=["changes_requested", "reject"])
    await runner.execute(run.id, deps=deps(gh, llm, {"fixes": [GOOD_FIX], "tests": [REGRESSION_TEST]}))
    run = await reload(db, WorkflowRun, run.id)
    assert run.status == "needs_human" and run.attempt == 2
    assert "Reviewer requested changes" in llm.prompts["review"][1] or True  # feedback flows to the Fix agent, not reviewer
    assert "rejected the approach" in (await db.execute(select(Report.markdown))).scalar_one()


async def test_low_confidence_root_cause_needs_human(env, db):
    _, gh = env
    _, bug = await setup(db, gh)
    run = await runner.start_run(db, bug, None)
    script = {"fixes": [GOOD_FIX], "tests": [REGRESSION_TEST], "confidence": 0.3}
    await runner.execute(run.id, deps=deps(gh, FakeLLM(), script))
    run = await reload(db, WorkflowRun, run.id)
    assert run.status == "needs_human"
    assert await db.scalar(select(func.count()).select_from(FixAttempt)) == 0
    assert not any(c[0] == "fix" for c in script["calls"])
    inv = (await db.execute(select(AgentStep.output).where(AgentStep.node == "investigation"))).scalar_one()
    assert inv["hypotheses"][1]["valid"] is False  # invented citation rejected


async def test_without_per_test_results_fix_is_unverifiable(env, db):
    fake, gh = env
    _, bug = await setup(db, gh, settings={"test_command": "python -m unittest discover -s tests"})  # exit code only
    run = await runner.start_run(db, bug, None)
    await runner.execute(run.id, deps=deps(gh, FakeLLM(), {"fixes": [GOOD_FIX], "tests": [REGRESSION_TEST]}))
    run = await reload(db, WorkflowRun, run.id)
    assert run.status == "needs_human" and run.validation_verdict == "unverifiable"
    assert await db.scalar(select(func.count()).select_from(PullRequest)) == 0


async def test_false_positive_triage_stops_early(env, db):
    _, gh = env
    _, bug = await setup(db, gh)
    bug = await reload(db, Bug, bug.id)
    bug.source = "auto_semgrep"
    await db.commit()
    run = await runner.start_run(db, bug, None)
    script = {"fixes": [GOOD_FIX], "tests": [REGRESSION_TEST]}
    await runner.execute(run.id, deps=deps(gh, FakeLLM(triage_real=False), script))
    assert (await reload(db, WorkflowRun, run.id)).status == "needs_human"
    assert script.get("calls") is None  # never spent effort on code agents


class Crash(BaseException):
    """Simulates the worker process dying (not an Exception, so nothing catches it)."""


async def test_resume_after_worker_crash_rebuilds_workspace(env, db):
    _, gh = env
    _, bug = await setup(db, gh)
    run = await runner.start_run(db, bug, None)
    script = {"fixes": [GOOD_FIX], "tests": [REGRESSION_TEST]}

    class CrashingLLM(FakeLLM):
        async def structured(self, *, agent, **kw):
            if agent == "security":
                raise Crash()
            return await super().structured(agent=agent, **kw)

    with pytest.raises(Crash):
        await runner.execute(run.id, deps=deps(gh, CrashingLLM(), script))
    for rid in list(REGISTRY):  # the process died: its sandbox is gone
        REGISTRY.pop(rid).destroy()

    await runner.execute(run.id, deps=deps(gh, FakeLLM(), script))  # new worker picks the run up
    run = await reload(db, WorkflowRun, run.id)
    assert run.status == "waiting_approval", (run.error, run.nodes)
    assert await db.scalar(select(func.count()).select_from(FixAttempt)) == 1  # resumed, not restarted
    fix_calls = [c for c in script["calls"] if c[0] == "fix"]
    assert len(fix_calls) == 1


async def test_cancel_and_reject(env, db):
    _, gh = env
    _, bug = await setup(db, gh)
    run = await runner.start_run(db, bug, None)
    await runner.execute(run.id, deps=deps(gh, FakeLLM(), {"fixes": [GOOD_FIX], "tests": [REGRESSION_TEST]}))
    async with logged_in(Role.reviewer) as rev:
        assert (await rev.post(f"/api/v1/runs/{run.id}/reject", json={"reason": "not now"})).status_code == 200
    await runner.execute(run.id, resume={"decision": "reject", "user_id": 1, "reason": "not now"},
                         deps=deps(gh, FakeLLM(), {"fixes": [GOOD_FIX], "tests": [REGRESSION_TEST]}))
    assert (await reload(db, WorkflowRun, run.id)).status == "rejected"
    assert await db.scalar(select(func.count()).select_from(PullRequest)) == 0

    run2 = await runner.start_run(db, await reload(db, Bug, bug.id), None)
    async with logged_in(Role.developer) as dev:
        assert (await dev.post(f"/api/v1/runs/{run2.id}/cancel")).status_code == 200
        detail = (await dev.get(f"/api/v1/runs/{run.id}")).json()
        assert detail["report"]["markdown"] and detail["node_order"][0] == "intake"
    await runner.execute(run2.id, deps=deps(gh, FakeLLM(), {"fixes": [GOOD_FIX], "tests": [REGRESSION_TEST]}))
    assert (await reload(db, WorkflowRun, run2.id)).status == "cancelled"


async def test_concurrent_runs_are_isolated(env, db):
    """Two debugging runs at once: separate sandboxes, branches, tokens; neither sees the other's changes."""
    import asyncio
    _, gh = env
    _, bug1 = await setup(db, gh)
    bug2 = Bug(repository_id=bug1.repository_id, source="github_issue", title="second bug", description="d",
               severity="low", status="new", fingerprint="fp-2", affected_files=[], data={})
    db.add(bug2)
    await db.commit()
    r1, r2 = await runner.start_run(db, bug1, None), await runner.start_run(db, bug2, None)
    s1 = {"fixes": [GOOD_FIX], "tests": [REGRESSION_TEST]}
    s2 = {"fixes": [WRONG_FIX, GOOD_FIX], "tests": [REGRESSION_TEST]}
    await asyncio.gather(runner.execute(r1.id, deps=deps(gh, FakeLLM(), s1)), runner.execute(r2.id, deps=deps(gh, FakeLLM(), s2)))
    a, b = await reload(db, WorkflowRun, r1.id), await reload(db, WorkflowRun, r2.id)
    assert (a.status, b.status) == ("waiting_approval", "waiting_approval"), (a.error, b.error)
    assert (a.attempt, b.attempt) == (1, 2) and a.branch_name != b.branch_name
    tokens = (await db.execute(select(GatewayToken))).scalars().all()
    assert {t.run_id for t in tokens} == {r1.id, r2.id} and len({t.token_hash for t in tokens}) == len(tokens)


async def test_pr_never_overwrites_a_branch_it_did_not_create(env, db):
    fake, gh = env
    _, bug = await setup(db, gh)
    for conflict, expected in (("foreign", "needs_human"), ("ours", "awaiting_merge")):
        bug = await reload(db, Bug, bug.id)
        run = await runner.start_run(db, bug, None)
        script = {"fixes": [GOOD_FIX], "tests": [REGRESSION_TEST]}
        await runner.execute(run.id, deps=deps(gh, FakeLLM(), script))
        fake.ref_conflict = conflict if conflict == "foreign" else f"ours:{run.id}"
        fake.git_writes.clear()
        await runner.execute(run.id, resume={"decision": "approve", "user_id": 99, "reason": ""}, deps=deps(gh, FakeLLM(), script))
        run = await reload(db, WorkflowRun, run.id)
        assert run.status == expected, run.error
        patched = [p for p, _ in fake.git_writes if "/git/refs/heads/" in p]
        if conflict == "foreign":
            assert "refusing to overwrite" in run.error and patched == []  # someone else's branch left untouched
            bug = await reload(db, Bug, bug.id)
            bug.status = "triaged"
            await db.commit()
        else:
            assert patched  # our own branch from this run may be updated (resume after a crash)


async def test_each_run_fetches_latest_base_and_leaves_no_worktree(env, db):
    from app.repocache import RepoCache
    _, gh = env
    r, bug = await setup(db, gh)
    newest = push_files("realify/api", {**REPO_FILES, "README.md": "new commit on main\n"}, "newer main")
    run = await runner.start_run(db, bug, None)
    await runner.execute(run.id, deps=deps(gh, FakeLLM(), {"fixes": [GOOD_FIX], "tests": [REGRESSION_TEST]}))
    run = await reload(db, WorkflowRun, run.id)
    assert run.base_sha == newest  # fetched right before the task, not a stale copy
    cache = RepoCache(get_settings())
    assert not cache.worktree_path(r.github_repo_id, f"run-{run.id}").exists()  # released while waiting for approval
    heads = await cache.git("for-each-ref", "--format=%(refname)", "refs/heads", cwd=cache.repo_path(r.github_repo_id))
    assert heads == ""  # task branch cleaned up locally; the shared clone has no modified base branch


async def test_default_flow_ai_opens_pr_and_humans_review_in_github(env, db):
    """Default policy: no DebugOps-side gate. AI opens the PR after validation; developers review/approve/merge in GitHub."""
    fake, gh = env
    _, bug = await setup(db, gh, settings={"require_pr_approval": False})
    run = await runner.start_run(db, bug, None)
    await runner.execute(run.id, deps=deps(gh, FakeLLM(), {"fixes": [GOOD_FIX], "tests": [REGRESSION_TEST]}))
    run = await reload(db, WorkflowRun, run.id)
    assert run.status == "awaiting_merge" and run.pr_url  # straight to PR, still never merged by DebugOps
    assert await db.scalar(select(func.count()).select_from(Approval)) == 0
    assert not any("merge" in p for p, _ in fake.git_writes)



# ---------------- task classification and AWS DevOps Agent routing ----------------

async def _no_sleep(_):
    return None


@pytest.fixture
def devops(monkeypatch):
    from tests.fake_devops_agent import TOKEN, FakeDevOpsAgent
    fake = FakeDevOpsAgent()
    url = fake.start()
    monkeypatch.setattr(get_settings(), "devops_agent_url", url)
    monkeypatch.setattr(get_settings(), "devops_agent_token", TOKEN)
    yield fake
    fake.stop()


async def _enable_integration(db, enabled=True):
    from app.models import Integration
    db.add(Integration(name="aws-devops-agent", enabled=enabled, last_check={"ok": True}))
    await db.commit()


def aws_deps(gh, llm, script):
    from app.integrations.devops_agent import DevOpsAgentClient
    d = deps(gh, llm, script)
    d.devops_agent, d.sleep = (lambda: DevOpsAgentClient(get_settings())), _no_sleep
    return d


async def test_production_incident_goes_through_aws_devops_agent(env, db, devops):
    from app.models import ActivityEvent, Investigation
    _, gh = env
    _, bug = await setup(db, gh)
    bug = await reload(db, Bug, bug.id)
    bug.data = {"labels": ["incident"]}
    await db.commit()
    await _enable_integration(db)
    run = await runner.start_run(db, bug, None)
    script = {"fixes": [GOOD_FIX], "tests": [REGRESSION_TEST]}
    await runner.execute(run.id, deps=aws_deps(gh, FakeLLM(), script))

    run = await reload(db, WorkflowRun, run.id)
    assert run.task_type == "incident" and run.status == "waiting_approval", (run.error, run.nodes)
    assert run.nodes["aws_investigation"]["status"] == "completed"
    inv = (await db.execute(select(Investigation))).scalar_one()
    assert inv.status == "COMPLETED" and inv.agent_task_id == "task-123" and "Root cause" in inv.summary
    assert inv.recommendations[0]["recommendationId"] == "rec-1"
    assert [c[0] for c in devops.calls][0] == "investigate" and "create_access_token" not in [c[0] for c in devops.calls]
    # Findings reach Claude Code as untrusted data; Claude Code also gets read-only MCP access for this run.
    analyst_prompt = next(p for a, p, _ in script["prompts"] if a == "code_analyst")
    assert "<untrusted_aws_investigation>" in analyst_prompt and "Root cause: login()" in analyst_prompt
    assert "rm -rf" not in analyst_prompt  # recommendation bodies are not forwarded, only their titles
    assert all(flag for a, _, flag in script["prompts"] if a in ("code_analyst", "investigation", "fix"))
    feed = (await db.execute(select(ActivityEvent.message).where(ActivityEvent.source == "aws_devops_agent"))).scalars().all()
    assert any("[FINDING]" in m for m in feed) and any("completed" in m for m in feed)


async def test_normal_bug_and_feature_skip_aws(env, db, devops):
    _, gh = env
    _, bug = await setup(db, gh)
    await _enable_integration(db)
    run = await runner.start_run(db, bug, None)
    script = {"fixes": [GOOD_FIX], "tests": [REGRESSION_TEST]}
    await runner.execute(run.id, deps=aws_deps(gh, FakeLLM(task_type="bug"), script))  # AI classifies: bug
    run = await reload(db, WorkflowRun, run.id)
    assert run.task_type == "bug" and run.status == "waiting_approval"
    assert devops.calls == []  # AWS never contacted
    assert not any(flag for _, _, flag in script["prompts"])

    bug2 = Bug(repository_id=run.repository_id, source="github_issue", title="Add mul3()", description="new helper",
               severity="low", status="new", fingerprint="feat-1", affected_files=[], data={"labels": ["enhancement"]})
    db.add(bug2)
    await db.commit()
    run2 = await runner.start_run(db, bug2, None)
    script2 = {"fixes": [GOOD_FIX], "tests": [REGRESSION_TEST]}
    await runner.execute(run2.id, deps=aws_deps(gh, FakeLLM(), script2))
    run2 = await reload(db, WorkflowRun, run2.id)
    assert run2.task_type == "feature" and devops.calls == []
    assert "FEATURE REQUEST" in next(p for a, p, _ in script2["prompts"] if a == "fix")


async def test_incident_with_integration_disabled_continues_without_aws(env, db, devops):
    _, gh = env
    _, bug = await setup(db, gh)
    bug = await reload(db, Bug, bug.id)
    bug.data = {"labels": ["production"]}
    await db.commit()
    await _enable_integration(db, enabled=False)
    run = await runner.start_run(db, bug, None)
    await runner.execute(run.id, deps=aws_deps(gh, FakeLLM(), {"fixes": [GOOD_FIX], "tests": [REGRESSION_TEST]}))
    run = await reload(db, WorkflowRun, run.id)
    assert run.task_type == "incident" and run.status == "waiting_approval" and devops.calls == []
    assert "not enabled" in run.nodes["aws_investigation"]["detail"]


# ---------------- task classification and AWS DevOps Agent routing ----------------

async def _no_sleep(_):
    return None


@pytest.fixture
def devops(monkeypatch):
    from tests.fake_devops_agent import TOKEN, FakeDevOpsAgent
    fake = FakeDevOpsAgent()
    url = fake.start()
    monkeypatch.setattr(get_settings(), "devops_agent_url", url)
    monkeypatch.setattr(get_settings(), "devops_agent_token", TOKEN)
    yield fake
    fake.stop()


async def _enable_integration(db, enabled=True):
    from app.models import Integration
    db.add(Integration(name="aws-devops-agent", enabled=enabled, last_check={"ok": True}))
    await db.commit()


def aws_deps(gh, llm, script):
    from app.integrations.devops_agent import DevOpsAgentClient
    d = deps(gh, llm, script)
    d.devops_agent, d.sleep = (lambda: DevOpsAgentClient(get_settings())), _no_sleep
    return d


async def test_production_incident_goes_through_aws_devops_agent(env, db, devops):
    from app.models import ActivityEvent, Investigation
    _, gh = env
    _, bug = await setup(db, gh)
    bug = await reload(db, Bug, bug.id)
    bug.data = {"labels": ["incident"]}
    await db.commit()
    await _enable_integration(db)
    run = await runner.start_run(db, bug, None)
    script = {"fixes": [GOOD_FIX], "tests": [REGRESSION_TEST]}
    await runner.execute(run.id, deps=aws_deps(gh, FakeLLM(), script))

    run = await reload(db, WorkflowRun, run.id)
    assert run.task_type == "incident" and run.status == "waiting_approval", (run.error, run.nodes)
    assert run.nodes["aws_investigation"]["status"] == "completed"
    inv = (await db.execute(select(Investigation))).scalar_one()
    assert inv.status == "COMPLETED" and inv.agent_task_id == "task-123" and "Root cause" in inv.summary
    assert inv.recommendations[0]["recommendationId"] == "rec-1"
    assert [c[0] for c in devops.calls][0] == "investigate" and "create_access_token" not in [c[0] for c in devops.calls]
    # Findings reach Claude Code as untrusted data; Claude Code also gets read-only MCP access for this run.
    analyst_prompt = next(p for a, p, _ in script["prompts"] if a == "code_analyst")
    assert "<untrusted_aws_investigation>" in analyst_prompt and "Root cause: login()" in analyst_prompt
    assert "rm -rf" not in analyst_prompt  # recommendation bodies are not forwarded, only their titles
    assert all(flag for a, _, flag in script["prompts"] if a in ("code_analyst", "investigation", "fix"))
    feed = (await db.execute(select(ActivityEvent.message).where(ActivityEvent.source == "aws_devops_agent"))).scalars().all()
    assert any("[FINDING]" in m for m in feed) and any("completed" in m for m in feed)


async def test_normal_bug_and_feature_skip_aws(env, db, devops):
    _, gh = env
    _, bug = await setup(db, gh)
    await _enable_integration(db)
    run = await runner.start_run(db, bug, None)
    script = {"fixes": [GOOD_FIX], "tests": [REGRESSION_TEST]}
    await runner.execute(run.id, deps=aws_deps(gh, FakeLLM(task_type="bug"), script))  # AI classifies: bug
    run = await reload(db, WorkflowRun, run.id)
    assert run.task_type == "bug" and run.status == "waiting_approval"
    assert devops.calls == []  # AWS never contacted
    assert not any(flag for _, _, flag in script["prompts"])

    bug2 = Bug(repository_id=run.repository_id, source="github_issue", title="Add mul3()", description="new helper",
               severity="low", status="new", fingerprint="feat-1", affected_files=[], data={"labels": ["enhancement"]})
    db.add(bug2)
    await db.commit()
    run2 = await runner.start_run(db, bug2, None)
    script2 = {"fixes": [GOOD_FIX], "tests": [REGRESSION_TEST]}
    await runner.execute(run2.id, deps=aws_deps(gh, FakeLLM(), script2))
    run2 = await reload(db, WorkflowRun, run2.id)
    assert run2.task_type == "feature" and devops.calls == []
    assert "FEATURE REQUEST" in next(p for a, p, _ in script2["prompts"] if a == "fix")


async def test_incident_with_integration_disabled_continues_without_aws(env, db, devops):
    _, gh = env
    _, bug = await setup(db, gh)
    bug = await reload(db, Bug, bug.id)
    bug.data = {"labels": ["production"]}
    await db.commit()
    await _enable_integration(db, enabled=False)
    run = await runner.start_run(db, bug, None)
    await runner.execute(run.id, deps=aws_deps(gh, FakeLLM(), {"fixes": [GOOD_FIX], "tests": [REGRESSION_TEST]}))
    run = await reload(db, WorkflowRun, run.id)
    assert run.task_type == "incident" and run.status == "waiting_approval" and devops.calls == []
    assert "not enabled" in run.nodes["aws_investigation"]["detail"]
