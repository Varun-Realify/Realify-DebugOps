"""The debugging workflow as a LangGraph state machine.

intake -> context -> analysis -> investigation -> root_cause
  -> fix -> testing -> validation -> security -> review -> report -> await_approval -> open_pr
Failed validation / security / review loops back to fix (bounded by max_fix_attempts). Every path ends in
`report`. Decisions about "fixed" come from the platform's own test runs, never from an agent's claim.
"""

import asyncio
import json
import logging
import secrets
import shlex
import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.errors import GraphBubbleUp
from langgraph.types import interrupt
from sqlalchemy import select

from app.agents import prompts
from app.agents.claude_code import EDIT_TOOLS, READ_TOOLS, CodeAgent
from app.agents.llm import LLM, AgentError
from app.agents.schemas import CodeAnalysis, FixResult, Investigation, ReviewVerdict, SecurityReview, TestingResult, Triage
from app.config import get_settings
from app.db import SessionLocal
from app.detection import parsers
from app.detection.scan import detect_suites
from app.events import log_event
from app.github.client import GitHubApp
from app.models import (AgentStep, Bug, Evidence, FixAttempt, GatewayToken, GitHubInstallation, Report, Repository,
                        TestResult, TestRun, WorkflowRun)
from app.repocache import RepoCache
from app.sandbox.runner import REPORTS, Sandbox, repack_tarball
from app.schemas import RepoSettings
from app.workflow.workspace import REGISTRY, SuiteRun, Workspace, WorkspaceError, is_test_path

log = logging.getLogger("debugops.workflow")


class RunState(TypedDict, total=False):
    run_id: int
    attempt: int
    triage: dict | None
    analysis: dict | None
    investigation: dict | None
    root_cause: dict | None
    feedback: list[str]
    fix: dict | None
    tests: dict | None
    attempt_id: int | None
    validation: dict | None
    security: dict | None
    review: dict | None
    outcome: str | None  # ready | needs_human | failed
    outcome_reason: str
    approval: dict | None
    pr: dict | None


class RunCancelled(Exception):
    pass


@dataclass
class Deps:
    gh: GitHubApp
    llm: Callable[[], LLM]
    code_agent: Callable[[Workspace], CodeAgent]
    sandbox: Callable[[str], Sandbox]


# ---------------- helpers ----------------

async def bug_context(db, bug: Bug) -> dict:
    ev = (await db.execute(select(Evidence.content).where(Evidence.bug_id == bug.id).order_by(Evidence.id.desc()).limit(3))).scalars().all()
    return {"id": bug.id, "source": bug.source, "severity": bug.severity, "title": bug.title,
            "description": bug.description, "affected_files": bug.affected_files, "evidence": "\n---\n".join(ev)}


def test_commands(ws: Workspace) -> list[str]:
    return [s.test for s in ws.suites] or ["(no test suite detected)"]


def bash_rules(ws: Workspace) -> list[str]:
    """Claude Code permission rules allowing exactly the detected test commands (prefix match)."""
    rules = []
    for s in ws.suites:
        rules.append(f"Bash({s.test.split(' --')[0]}:*)")
    return rules


def verify_citations(ws: Workspace, inv: Investigation) -> list[dict]:
    """Drop evidence citing files or lines that do not exist at the base commit."""
    verified = []
    for h in inv.hypotheses:
        good, bad = [], []
        for ev in h.evidence:
            path = ev.file.lstrip("./")
            lines = ws.source.read_lines(path, 1, 10**7) if path in ws.source.files else ""
            n = len(lines.splitlines()) if lines else 0
            (good if path in ws.source.files and 1 <= ev.line <= max(n, 1) else bad).append({**ev.model_dump(), "file": path})
        verified.append({**h.model_dump(), "evidence": good, "rejected_evidence": bad,
                         "valid": bool(good) and len(bad) <= len(good)})
    return verified


def classify(changes: list[tuple[str, str]]) -> tuple[list, list]:
    tests = [(st, p) for st, p in changes if is_test_path(p)]
    code = [(st, p) for st, p in changes if not is_test_path(p)]
    return code, tests


async def mint_gateway_token(db, run_id: int, hours: int = 6) -> str:
    token = "dgw_" + secrets.token_urlsafe(32)
    db.add(GatewayToken(token_hash=hashlib.sha256(token.encode()).hexdigest(), run_id=run_id,
                        expires_at=datetime.now(UTC) + timedelta(hours=hours)))
    await db.commit()
    return token


async def revoke_gateway_tokens(db, run_id: int) -> None:
    from sqlalchemy import update
    await db.execute(update(GatewayToken).where(GatewayToken.run_id == run_id, GatewayToken.revoked_at.is_(None))
                     .values(revoked_at=datetime.now(UTC)))


def live(run_id: int, source: str):
    """Callback that publishes one live-feed item immediately (own transaction -> NOTIFY -> WebSocket)."""
    async def emit(kind: str, message: str) -> None:
        async with SessionLocal() as db:
            log_event(db, source, message, type=kind, run_id=run_id)
            await db.commit()
    return emit


def release_workspace(run_id: int) -> None:
    ws = REGISTRY.pop(run_id, None)
    if ws is not None:
        try:
            ws.destroy()
        except Exception:
            log.exception("failed to destroy workspace for run %s", run_id)


async def release_worktree(db, run_id: int) -> None:
    """Remove the run's worktree and local task branch (the PR branch, if any, lives on GitHub)."""
    run = await db.get(WorkflowRun, run_id)
    repo = await db.get(Repository, run.repository_id)
    try:
        await RepoCache(get_settings()).remove_worktree(repo.github_repo_id, f"run-{run_id}", delete_branch=run.branch_name)
    except Exception:
        log.exception("could not remove worktree for run %s", run_id)


def save_suite_run(db, run: WorkflowRun, phase: str, attempt: int | None, sr: SuiteRun, command: str) -> None:
    counts = sr.summary()
    tr = TestRun(run_id=run.id, attempt_no=attempt, commit_sha=run.base_sha, phase=phase, command=command,
                 exit_code=(sr.exit_codes or [None])[-1], timed_out=sr.timed_out,
                 result_format="junit" if sr.per_test else "exit_code",
                 passed=counts.get("passed", 0), failed=counts.get("failed", 0), errors=counts.get("error", 0),
                 skipped=counts.get("skipped", 0), log_tail=parsers.redact(sr.log)[-30000:])
    db.add(tr)
    return tr


# ---------------- graph ----------------

def build_graph(deps: Deps) -> StateGraph:
    s = get_settings()

    def node(name: str, agent: str):
        """Wrap a node: cancellation check, AgentStep + run.nodes bookkeeping, live events, cost accounting."""
        def deco(fn):
            async def wrapped(state: RunState) -> dict:
                async with SessionLocal() as db:
                    run = await db.get(WorkflowRun, state["run_id"])
                    if run.status == "cancelled":
                        raise RunCancelled()
                    # A node resumed after a human decision: close the step that was left waiting.
                    for w in (await db.execute(select(AgentStep).where(AgentStep.run_id == run.id, AgentStep.node == name,
                                                                      AgentStep.status == "waiting"))).scalars():
                        w.status, w.finished_at = "completed", datetime.now(UTC)
                    step = AgentStep(run_id=run.id, node=name, agent=agent, status="running", attempt=state.get("attempt", 0))
                    db.add(step)
                    run.current_node = name
                    run.nodes = {**run.nodes, name: {"status": "running", "started_at": datetime.now(UTC).isoformat(),
                                                     "attempt": state.get("attempt", 0)}}
                    log_event(db, agent, f"{agent.replace('_', ' ').title()} started: {name}", run_id=run.id, node=name)
                    await db.commit()
                    step_id, run_id = step.id, run.id  # usable after a rollback expires the objects
                    acct = {"in": 0, "out": 0, "cost": 0.0}
                    try:
                        update, output, detail = await fn(db, run, state, acct)
                    except GraphBubbleUp:  # interrupt(): waiting for a human, not a failure
                        step.status = "waiting"
                        run.nodes = {**run.nodes, name: {**run.nodes.get(name, {}), "status": "waiting"}}
                        log_event(db, agent, f"{name}: waiting for human approval", run_id=run.id, node=name)
                        await db.commit()
                        raise
                    except Exception as e:
                        await db.rollback()
                        run = await db.get(WorkflowRun, run_id, populate_existing=True)
                        step = await db.get(AgentStep, step_id, populate_existing=True)
                        step.status, step.error, step.finished_at = "failed", f"{type(e).__name__}: {e}"[:2000], datetime.now(UTC)
                        run.nodes = {**run.nodes, name: {**run.nodes.get(name, {}), "status": "failed", "detail": str(e)[:300]}}
                        log_event(db, agent, f"{name} failed: {e}", level="error", type="error", run_id=run.id, node=name)
                        await db.commit()
                        raise
                    step.status, step.finished_at, step.output = "completed", datetime.now(UTC), output
                    step.tokens_in, step.tokens_out, step.cost_usd = acct["in"], acct["out"], acct["cost"]
                    run.tokens_in += acct["in"]
                    run.tokens_out += acct["out"]
                    run.cost_usd = round(run.cost_usd + acct["cost"], 6)
                    run.nodes = {**run.nodes, name: {**run.nodes.get(name, {}), "status": "completed", "detail": detail[:300],
                                                     "finished_at": datetime.now(UTC).isoformat()}}
                    log_event(db, agent, f"{name}: {detail}"[:1000], run_id=run.id, node=name)
                    await db.commit()
                    return update
            wrapped.__name__ = name
            return wrapped
        return deco

    def budget_check(run: WorkflowRun):
        if run.tokens_in + run.tokens_out > s.run_max_llm_tokens:
            raise AgentError(f"run token budget exhausted ({s.run_max_llm_tokens:,} tokens)")

    def add_usage(acct: dict, result) -> None:
        acct["in"] += result.tokens_in
        acct["out"] += result.tokens_out
        acct["cost"] += result.cost_usd

    async def workspace(db, run: WorkflowRun, state: RunState) -> Workspace:
        """Live workspace for the run; rebuilt from GitHub + stored attempt if this process does not have it."""
        if run.id in REGISTRY:
            return REGISTRY[run.id]
        repo = await db.get(Repository, run.repository_id)
        inst = await db.get(GitHubInstallation, repo.installation_id)
        token = await deps.gh.installation_token(inst.installation_id, repository_ids=[repo.github_repo_id],
                                                 permissions={"contents": "read", "metadata": "read"})
        cache = RepoCache(s)
        await cache.sync(repo.github_repo_id, repo.full_name, token)  # always fetch the latest code first
        if not run.base_sha:
            run.base_sha = await cache.resolve(repo.github_repo_id, repo.default_branch)
            await db.commit()
        # Isolated worktree on the task branch; the shared clone's base branch is never modified.
        wt = await cache.add_worktree(repo.github_repo_id, f"run-{run.id}", run.base_sha, branch=run.branch_name)
        archive = await cache.archive(wt)
        try:
            source = await asyncio.to_thread(repack_tarball, archive, s.max_repo_bytes, s.max_repo_files)
        finally:
            archive.close()
        pkg = None
        if "package.json" in source.files:
            try:
                pkg = json.loads(source.read_lines("package.json", 1, 10**6) or "{}")
            except ValueError:
                pkg = None
        suites = detect_suites(source.files, pkg, RepoSettings.of(repo.settings))
        sb = deps.sandbox(f"run{run.id}")
        await asyncio.to_thread(sb.start, source)
        ws = Workspace(run.id, sb, source, run.base_sha, suites, s, run.branch_name)
        try:
            await ws.setup()
            ws.gateway_token = await mint_gateway_token(db, run.id)
            if state.get("attempt_id"):  # resumed after a worker restart: rebuild fix/tests commits
                fa = await db.get(FixAttempt, state["attempt_id"])
                await ws.apply_files({p: fa.files.get(p) for p in fa.fix_paths})
                ws.refs["fix"] = await ws.commit("fix (restored)")
                await ws.apply_files({p: fa.files.get(p) for p in fa.test_paths})
                ws.refs["tests"] = await ws.commit("tests (restored)")
        except Exception:
            ws.destroy()
            raise
        REGISTRY[run.id] = ws
        return ws

    # ---- nodes ----

    @node("intake", "bug_hunter")
    async def intake(db, run, state, acct):
        bug = await db.get(Bug, run.bug_id)
        bug.status = "investigating"
        run.status, run.started_at = "running", run.started_at or datetime.now(UTC)
        await db.commit()
        if not bug.source.startswith("auto_"):
            return {"triage": None, "feedback": []}, {}, "reported by a human; triage skipped"
        budget_check(run)
        ctx = await bug_context(db, bug)
        res = await deps.llm().structured(agent="bug_hunter", system=prompts.TRIAGE_SYSTEM,
                                          prompt=prompts.bug_block(ctx), schema=Triage)
        add_usage(acct, res)
        t = res.parsed.model_dump()
        update: dict[str, Any] = {"triage": t, "feedback": []}
        if not res.parsed.is_real_bug and res.parsed.confidence >= 0.8:
            update |= {"outcome": "needs_human", "outcome_reason": f"Bug Hunter judged this a likely false positive: {res.parsed.rationale}"}
        return update, t, f"real bug: {res.parsed.is_real_bug} (confidence {res.parsed.confidence:.2f})"

    @node("context", "platform")
    async def context(db, run, state, acct):
        await live(run.id, "platform")("action", "Fetching the latest code into the persistent clone and preparing an isolated sandbox…")
        ws = await workspace(db, run, state)
        suites = ", ".join(f"{x.name}: {x.test}" for x in ws.suites) or "none detected"
        return {}, {"base_sha": run.base_sha, "files": len(ws.source.files), "suites": suites}, \
            f"{run.base_sha[:12]}, {len(ws.source.files)} files; tests: {suites}"

    @node("analysis", "code_analyst")
    async def analysis(db, run, state, acct):
        budget_check(run)
        ws = await workspace(db, run, state)
        bug = await bug_context(db, await db.get(Bug, run.bug_id))
        prompt = prompts.ANALYST.format(bug=prompts.bug_block(bug), schema=prompts.schema_instruction(CodeAnalysis))
        res = await deps.code_agent(ws).run(agent="code_analyst", prompt=prompt, schema=CodeAnalysis, tools=READ_TOOLS, edit=False,
                                            on_event=live(run.id, "code_analyst"))
        add_usage(acct, res)
        a = res.parsed.model_dump()
        return {"analysis": a}, a, f"{len(res.parsed.relevant_files)} relevant file(s)"

    @node("investigation", "investigation")
    async def investigation(db, run, state, acct):
        budget_check(run)
        ws = await workspace(db, run, state)
        bug = await bug_context(db, await db.get(Bug, run.bug_id))
        prompt = prompts.INVESTIGATOR.format(
            test_commands="\n".join(test_commands(ws)), analysis=json.dumps(state.get("analysis") or {})[:15000],
            bug=prompts.bug_block(bug), schema=prompts.schema_instruction(Investigation))
        res = await deps.code_agent(ws).run(agent="investigation", prompt=prompt, schema=Investigation,
                                            tools=READ_TOOLS + bash_rules(ws), edit=False, on_event=live(run.id, "investigation"))
        add_usage(acct, res)
        hyps = verify_citations(ws, res.parsed)
        out = {"hypotheses": hyps}
        return {"investigation": out}, out, f"{len(hyps)} hypothesis(es), {sum(h['valid'] for h in hyps)} with verified evidence"

    @node("root_cause", "platform")
    async def root_cause(db, run, state, acct):
        hyps = [h for h in (state.get("investigation") or {}).get("hypotheses", []) if h["valid"]]
        best = max(hyps, key=lambda h: h["confidence"], default=None)
        if best is None or best["confidence"] < s.root_cause_min_confidence:
            reason = ("No hypothesis had verifiable evidence" if best is None else
                      f"Best hypothesis confidence {best['confidence']:.2f} is below the {s.root_cause_min_confidence} threshold")
            return {"root_cause": best, "outcome": "needs_human", "outcome_reason": reason}, {"selected": best}, reason
        return {"root_cause": best}, {"selected": best}, f"confidence {best['confidence']:.2f}: {best['claim'][:200]}"

    @node("fix", "fix")
    async def fix(db, run, state, acct):
        budget_check(run)
        attempt = state.get("attempt", 0) + 1
        run.attempt = attempt
        await db.commit()
        ws = await workspace(db, run, state)
        await ws.reset_to("base")
        ws.refs = {}
        bug = await bug_context(db, await db.get(Bug, run.bug_id))
        feedback = state.get("feedback") or []
        prompt = prompts.FIXER.format(
            test_commands="; ".join(test_commands(ws)), root_cause=json.dumps(state.get("root_cause"))[:10000],
            bug=prompts.bug_block(bug), schema=prompts.schema_instruction(FixResult),
            feedback=("\nPrevious attempts failed. Address this feedback:\n" + "\n".join(f"- {f}" for f in feedback[-6:])) if feedback else "")
        res = await deps.code_agent(ws).run(agent="fix", prompt=prompt, schema=FixResult, tools=EDIT_TOOLS + bash_rules(ws), edit=True,
                                            on_event=live(run.id, "fix"))
        add_usage(acct, res)
        code, tests = classify(await ws.changed_since("base"))
        violations = []
        if tests:  # the Fix agent may not touch tests: revert, and say so
            await ws.revert_paths("base", tests)
            violations.append(f"Fix agent modified test files (reverted): {', '.join(p for _, p in tests)}")
        if not code:
            fb = "The fix attempt changed no source files."
            return {"attempt": attempt, "fix": {"ok": False, "violations": violations},
                    "feedback": feedback + violations + [fb]}, {"ok": False}, fb
        ws.refs["fix"] = await ws.commit(f"fix attempt {attempt}")
        out = {"ok": True, "summary": res.parsed.summary, "rationale": res.parsed.rationale,
               "paths": [p for _, p in code], "violations": violations}
        return {"attempt": attempt, "fix": out, "feedback": feedback + violations}, out, \
            f"attempt {attempt}: changed {', '.join(out['paths'])[:200]}"

    @node("testing", "testing")
    async def testing(db, run, state, acct):
        budget_check(run)
        ws = await workspace(db, run, state)
        bug = await bug_context(db, await db.get(Bug, run.bug_id))
        fix_diff = await ws.diff("base", ws.refs["fix"])
        hint = ", ".join(sorted({p.split("/")[0] for p in ws.source.files if is_test_path(p)})[:5]) or "tests/"
        prompt = prompts.TESTER.format(
            test_hint=hint, test_commands="; ".join(test_commands(ws)), root_cause=json.dumps(state.get("root_cause"))[:8000],
            fix_diff=prompts.untrusted("diff", fix_diff, 30000), bug=prompts.bug_block(bug),
            schema=prompts.schema_instruction(TestingResult),
            feedback=("\nFeedback from previous attempts:\n" + "\n".join(f"- {f}" for f in (state.get('feedback') or [])[-6:])) if state.get("feedback") else "")
        res = await deps.code_agent(ws).run(agent="testing", prompt=prompt, schema=TestingResult,
                                            tools=EDIT_TOOLS + bash_rules(ws), edit=True, on_event=live(run.id, "testing"))
        add_usage(acct, res)
        code, tests = classify(await ws.changed_since(ws.refs["fix"]))
        violations = []
        if code:  # the Testing agent may only write tests
            await ws.revert_paths(ws.refs["fix"], code)
            violations.append(f"Testing agent changed non-test files (reverted): {', '.join(p for _, p in code)}")
        deleted = [p for st, p in tests if st == "D"]
        if deleted:
            await ws.revert_paths(ws.refs["fix"], [(st, p) for st, p in tests if st == "D"])
            tests = [(st, p) for st, p in tests if st != "D"]
            violations.append(f"Testing agent deleted tests (restored): {', '.join(deleted)}")
        ws.refs["tests"] = await ws.commit(f"regression tests attempt {state['attempt']}")
        files = await ws.files_at("base", ws.refs["tests"])
        fa = FixAttempt(run_id=run.id, attempt_no=state["attempt"], diff=await ws.diff("base", ws.refs["tests"]), files=files,
                        fix_paths=[p for p in files if not is_test_path(p)], test_paths=[p for p in files if is_test_path(p)],
                        rationale=(state.get("fix") or {}).get("rationale", ""), status="pending")
        db.add(fa)
        await db.flush()
        out = {"ok": bool(tests), "paths": [p for _, p in tests], "summary": res.parsed.summary, "violations": violations}
        fb = list(state.get("feedback") or []) + violations
        if not tests:
            fb.append("No regression test was written.")
        return {"tests": out, "attempt_id": fa.id, "feedback": fb}, out, \
            f"{len(tests)} test file(s): {', '.join(out['paths'])[:200]}" if tests else "no regression test written"

    @node("validation", "platform")
    async def validation(db, run, state, acct):
        ws = await workspace(db, run, state)
        fa = await db.get(FixAttempt, state["attempt_id"])
        attempt = state["attempt"]
        cmd = "; ".join(x.test for x in ws.suites)

        def verdict(v: str, reason: str, **extra) -> tuple:
            result = {"verdict": v, "reason": reason, **extra}
            fa.validation = result
            run.validation_verdict = v
            fb = list(state.get("feedback") or [])
            if v == "failed":
                fb.append(f"Validation failed: {reason}")
            return {"validation": result, "feedback": fb}, result, f"{v}: {reason}"

        if not ws.suites:
            return verdict("unverifiable", "no test suite detected; configure test_command in repository settings")
        if not fa.test_paths:
            return verdict("failed", "no regression test was written")

        say = live(run.id, "platform")

        def counts(sr: SuiteRun) -> str:
            c = sr.summary()
            return f"{c.get('passed', 0)} passed, {c.get('failed', 0)} failed, {c.get('error', 0)} errors"

        if ws.baseline is None:
            await say("test", "Running the test suite on the original code (baseline)…")
            ws.baseline = await ws.run_tests_at("base")
            save_suite_run(db, run, "baseline", None, ws.baseline, cmd)
            await say("test", f"Baseline: {counts(ws.baseline)}")
        r0 = ws.baseline
        await say("test", "Running the new regression test against the ORIGINAL code (it must fail)…")
        r1 = await ws.run_tests_at("base", overlay_from=ws.refs["tests"], overlay_paths=[p for p in fa.test_paths if fa.files.get(p) is not None])
        await say("test", f"Original code + new test: {counts(r1)}")
        await say("test", "Running the full suite WITH the fix (the new test must pass, nothing may break)…")
        r2 = await ws.run_tests_at(ws.refs["tests"])
        await say("test", f"With the fix: {counts(r2)}")
        await ws.reset_to(ws.refs["tests"])
        save_suite_run(db, run, "base", attempt, r1, cmd)
        tr2 = save_suite_run(db, run, "fix", attempt, r2, cmd)
        await db.flush()
        new_ids = sorted(set(r1.results) - set(r0.results))
        for tid in new_ids:
            db.add(TestResult(test_run_id=tr2.id, test_id=tid[:1000], status=r2.results.get(tid, "missing"), is_regression=True))
        evidence = {"baseline": r0.summary(), "base_with_new_tests": r1.summary(), "with_fix": r2.summary(),
                    "regression_tests": {t: {"before_fix": r1.results.get(t), "after_fix": r2.results.get(t)} for t in new_ids}}
        if not (r0.per_test and r1.per_test and r2.per_test):
            return verdict("unverifiable", "the test suite does not produce per-test results (JUnit/Jest); "
                           "cannot prove the regression test fails before and passes after", evidence=evidence)
        failing_before = [t for t in new_ids if r1.results[t] in ("failed", "error")]
        if not new_ids:
            return verdict("failed", "the regression test file produced no new test cases", evidence=evidence)
        if not failing_before:
            return verdict("failed", "the regression test passes on the original code, so it does not reproduce the bug", evidence=evidence)
        still = [t for t in failing_before if r2.results.get(t) != "passed"]
        if still:
            return verdict("failed", f"regression test still fails with the fix: {', '.join(still[:5])}", evidence=evidence)
        broken = [t for t, st in r0.results.items() if st == "passed" and r2.results.get(t, "missing") in ("failed", "error", "missing")]
        if broken:
            return verdict("failed", f"fix breaks previously passing tests: {', '.join(broken[:10])}", evidence=evidence)
        return verdict("verified", f"{len(failing_before)} regression test(s) fail before the fix and pass after; "
                       f"no previously passing test broke ({len(r2.results)} tests run)", evidence=evidence)

    @node("security", "security")
    async def security(db, run, state, acct):
        budget_check(run)
        ws = await workspace(db, run, state)
        fa = await db.get(FixAttempt, state["attempt_id"])
        changed = [p for p, c in fa.files.items() if c is not None]

        async def scan_at(ref: str) -> dict[str, parsers.Finding]:
            await ws.git(f"checkout -qf --detach {shlex.quote(ref)}")
            present = [p for p in changed if (await ws.sh(f"test -f {shlex.quote(p)}", check=False)).exit_code == 0]
            found: list[parsers.Finding] = []
            if present:
                await ws.sh(f"semgrep scan --config /opt/rules/semgrep-default.yml --json --metrics=off --disable-version-check -q "
                            f"--output {REPORTS}/sec-semgrep.json " + " ".join(shlex.quote(p) for p in present), timeout=600, check=False)
                raw = await asyncio.to_thread(ws.sb.read_file, f"{REPORTS}/sec-semgrep.json")
                found += parsers.parse_semgrep((raw or b"").decode(), lambda p, a, b: ws.source.read_lines(p, a, b))
                for p in present:
                    await ws.sh(f"gitleaks dir {shlex.quote(p)} --report-format json --report-path {REPORTS}/sec-gl.json "
                                "--no-banner --exit-code 0", check=False)
                    raw = await asyncio.to_thread(ws.sb.read_file, f"{REPORTS}/sec-gl.json")
                    found += parsers.parse_gitleaks((raw or b"").decode() or "[]")
            return {f.fingerprint: f for f in found}

        await live(run.id, "security")("search", f"Scanning {len(changed)} changed file(s) with Semgrep and Gitleaks, before and after the change…")
        before, after = await scan_at("base"), await scan_at(ws.refs["tests"])
        await ws.reset_to(ws.refs["tests"])
        introduced = [f for fp, f in after.items() if fp not in before]
        res = await deps.llm().structured(
            agent="security", system=prompts.SECURITY_SYSTEM, schema=SecurityReview,
            prompt=prompts.untrusted("diff", fa.diff, 60000) + "\n\nNew scanner findings in the changed files:\n" +
            json.dumps([{"title": f.title, "severity": f.severity, "file": f.file_path, "line": f.line_start} for f in introduced]))
        add_usage(acct, res)
        sr = res.parsed
        # Agent output is advisory unless it is concrete: a high/critical issue located in a changed file.
        agent_blockers = [i for i in sr.issues if i.severity in ("high", "critical") and i.file.lstrip("./") in fa.files]
        scanner_blockers = [f for f in introduced if f.severity in ("high", "critical")]
        blocked = bool(agent_blockers or scanner_blockers)
        result = {"verdict": "block" if blocked else "pass", "agent": sr.model_dump(),
                  "introduced_findings": [{"title": f.title, "severity": f.severity, "file": f.file_path, "line": f.line_start,
                                           "source": f.source} for f in introduced],
                  "blockers": [i.model_dump() for i in agent_blockers] + [{"title": f.title, "severity": f.severity} for f in scanner_blockers]}
        fa.security = result
        fb = list(state.get("feedback") or [])
        if blocked:
            fb.append("Security review blocked the change: " + "; ".join(
                [f"{i.file}:{i.line} {i.description}" for i in agent_blockers] + [f.title for f in scanner_blockers])[:1500])
        return {"security": result, "feedback": fb}, result, \
            f"{result['verdict']}: {len(introduced)} new scanner finding(s), {len(sr.issues)} agent issue(s)"

    @node("review", "review")
    async def review(db, run, state, acct):
        budget_check(run)
        fa = await db.get(FixAttempt, state["attempt_id"])
        bug = await bug_context(db, await db.get(Bug, run.bug_id))
        # Deliberately excludes the Fix agent's rationale: the reviewer judges the change on its own merits.
        prompt = (prompts.bug_block(bug) + "\n\n" + prompts.untrusted("diff", fa.diff, 80000) +
                  "\n\nPlatform test evidence (produced by DebugOps, not by an agent):\n" + json.dumps(fa.validation)[:20000] +
                  "\n\nSecurity results:\n" + json.dumps(fa.security)[:10000])
        res = await deps.llm().structured(agent="review", system=prompts.REVIEW_SYSTEM, prompt=prompt, schema=ReviewVerdict)
        add_usage(acct, res)
        rv = res.parsed.model_dump()
        fa.review = rv
        fb = list(state.get("feedback") or [])
        if res.parsed.verdict == "changes_requested":
            fb.append("Reviewer requested changes: " + "; ".join(f"{i.file}:{i.line} {i.description}" for i in res.parsed.issues)[:2000])
        update: dict[str, Any] = {"review": rv, "feedback": fb}
        if res.parsed.verdict == "approve":
            fa.status = "accepted"
            update |= {"outcome": "ready", "outcome_reason": "validated, security-checked and approved by the review agent"}
        elif res.parsed.verdict == "reject":
            fa.status = "rejected"
            update |= {"outcome": "needs_human", "outcome_reason": f"Review agent rejected the approach: {res.parsed.summary}"}
        else:
            fa.status = "rejected"
        return update, rv, f"{res.parsed.verdict}: {res.parsed.summary[:200]}"

    @node("retry_gate", "platform")
    async def retry_gate(db, run, state, acct):
        if state.get("attempt_id"):
            fa = await db.get(FixAttempt, state["attempt_id"])
            if fa and fa.status == "pending":
                fa.status = "rejected"
        if state.get("attempt", 0) >= s.max_fix_attempts:
            reason = f"no validated fix after {state.get('attempt', 0)} attempt(s); last issue: {(state.get('feedback') or ['?'])[-1][:300]}"
            return {"outcome": "failed", "outcome_reason": reason}, {}, reason
        return {"attempt_id": None, "validation": None, "security": None, "review": None}, {}, \
            f"retrying (attempt {state.get('attempt', 0) + 1} of {s.max_fix_attempts})"

    @node("report", "platform")
    async def report(db, run, state, acct):
        from app.workflow.report import build_report
        outcome = state.get("outcome") or "needs_human"
        if outcome == "ready" and (state.get("validation") or {}).get("verdict") != "verified":
            outcome = "needs_human"  # never present an unvalidated fix as ready
        rep = await build_report(db, run, state, outcome)
        if state.get("attempt_id"):  # audit summary of the final attempt
            fa = await db.get(FixAttempt, state["attempt_id"])
            run.files_changed = sorted(fa.files)
            ev = (fa.validation or {}).get("evidence") or {}
            run.test_summary = {**(ev.get("with_fix") or {}), "verdict": (fa.validation or {}).get("verdict"),
                                "regression_tests": len(ev.get("regression_tests") or {})}
        bug = await db.get(Bug, run.bug_id)
        bug.status = "fix_proposed" if outcome == "ready" else "triaged"
        release_workspace(run.id)
        await release_worktree(db, run.id)
        await revoke_gateway_tokens(db, run.id)
        return {"outcome": outcome}, {"outcome": outcome, "reason": state.get("outcome_reason", "")}, \
            f"{outcome}: {state.get('outcome_reason', '')}"[:300]

    @node("await_approval", "platform")
    async def await_approval(db, run, state, acct):
        repo = await db.get(Repository, run.repository_id)
        if not RepoSettings.of(repo.settings).require_pr_approval:
            return {"approval": {"decision": "approve", "user_id": None, "auto": True}}, {"auto": True}, \
                "PR approval not required by repository settings (merge still requires a human)"
        run.status = "waiting_approval"
        await db.commit()
        decision = interrupt({"kind": "open_pr", "run_id": run.id})  # resumes with {"decision", "user_id", "reason"}
        run.status = "running"
        return {"approval": decision}, decision, f"{decision.get('decision')} by user #{decision.get('user_id')}"

    @node("open_pr", "platform")
    async def open_pr(db, run, state, acct):
        from app.workflow.pr import open_pull_request
        pr = await open_pull_request(db, deps.gh, run, state)
        return {"pr": pr}, pr, f"opened PR #{pr['number']}"

    # ---- routing ----
    def after(field: str):
        return lambda st: "report" if st.get("outcome") else field

    def route_fix(st):
        return "testing" if (st.get("fix") or {}).get("ok") else "retry_gate"

    def route_testing(st):
        return "validation" if (st.get("tests") or {}).get("ok") else "retry_gate"

    def route_validation(st):
        v = (st.get("validation") or {}).get("verdict")
        return {"verified": "security", "failed": "retry_gate"}.get(v, "report")

    g = StateGraph(RunState)
    for fn in (intake, context, analysis, investigation, root_cause, fix, testing, validation, security, review,
               retry_gate, report, await_approval, open_pr):
        g.add_node(fn.__name__, fn)
    g.add_edge(START, "intake")
    g.add_conditional_edges("intake", after("context"), ["context", "report"])
    g.add_edge("context", "analysis")
    g.add_edge("analysis", "investigation")
    g.add_edge("investigation", "root_cause")
    g.add_conditional_edges("root_cause", after("fix"), ["fix", "report"])
    g.add_conditional_edges("fix", route_fix, ["testing", "retry_gate"])
    g.add_conditional_edges("testing", route_testing, ["validation", "retry_gate"])
    g.add_conditional_edges("validation", route_validation, ["security", "retry_gate", "report"])
    g.add_conditional_edges("security", lambda st: "review" if st["security"]["verdict"] == "pass" else "retry_gate", ["review", "retry_gate"])
    g.add_conditional_edges("review", lambda st: "report" if st.get("outcome") else "retry_gate", ["report", "retry_gate"])
    g.add_conditional_edges("retry_gate", lambda st: "report" if st.get("outcome") else "fix", ["fix", "report"])
    g.add_conditional_edges("report", lambda st: "await_approval" if st.get("outcome") == "ready" else END, ["await_approval", END])
    g.add_conditional_edges("await_approval", lambda st: "open_pr" if (st.get("approval") or {}).get("decision") == "approve" else END,
                            ["open_pr", END])
    g.add_edge("open_pr", END)
    return g


NODE_ORDER = ["intake", "context", "analysis", "investigation", "root_cause", "fix", "testing", "validation",
              "security", "review", "retry_gate", "report", "await_approval", "open_pr"]
