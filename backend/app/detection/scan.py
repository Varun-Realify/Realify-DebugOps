"""Automatic bug detection: one scan = download source, run scanners + tests in a sandbox, record findings."""

import asyncio
import json
import logging
import shlex
import tarfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import queue
from app.config import get_settings
from app.detection import parsers
from app.detection.bugs import meets_threshold, record_findings
from app.events import log_event
from app.github.client import GitHubApp, GitHubError, GitHubNotConfigured
from app.models import Bug, GitHubInstallation, Repository, ScanRun, TestResult, TestRun
from app.repocache import RepoCache, RepoCacheError
from app.sandbox.runner import REPORTS, SRC, Sandbox, SandboxError, SourceTree, repack_tarball
from app.schemas import RepoSettings

log = logging.getLogger("debugops.scan")

SOURCE = "scanner"
VENV = "/workspace/.venv"


async def request_scan(db: AsyncSession, repo: Repository, trigger: str, user_id: int | None = None) -> ScanRun | None:
    """Queue a scan. Returns None if this repository already has one queued or running. Commits."""
    scan = ScanRun(repository_id=repo.id, trigger=trigger, requested_by=user_id, status="queued", ref=repo.default_branch)
    db.add(scan)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        return None
    await queue.enqueue(db, "scan.run", {"scan_id": scan.id}, max_attempts=1)  # a failed scan is re-requested, not retried blindly
    log_event(db, SOURCE, f"Scan #{scan.id} queued for {repo.full_name} ({trigger})")
    await db.commit()
    return scan


# ---------------- test command detection ----------------

@dataclass
class TestSuite:
    name: str
    install: list[str]  # shell commands run in order inside the sandbox
    test: str
    report: str | None  # junit | jest | None (exit code only)


def detect_suites(files: set[str], package_json: dict | None, settings: RepoSettings) -> list[TestSuite]:
    if settings.test_command:
        install = [settings.install_command] if settings.install_command else []
        return [TestSuite("custom", install, settings.test_command, "junit")]  # junit if the command writes reports/junit.xml

    suites = []
    py_markers = {"pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "pytest.ini", "tox.ini"}
    has_py_tests = any(f.endswith(".py") and (f.rsplit("/", 1)[-1].startswith("test_") or f.endswith("_test.py")) for f in files)
    if py_markers & files and has_py_tests:
        pip = f"{VENV}/bin/pip install -q"
        install = [f"python -m venv {VENV}", f"{pip} pytest"]
        for req in ("requirements.txt", "requirements-dev.txt", "dev-requirements.txt", "requirements/dev.txt", "requirements/test.txt"):
            if req in files:
                install.append(f"{pip} -r {shlex.quote(req)}")
        if "pyproject.toml" in files or "setup.py" in files:
            install.append(f"{pip} -e . || {pip} .")
        suites.append(TestSuite("python", install,
                                f"{VENV}/bin/python -m pytest -q -p no:cacheprovider --junitxml={REPORTS}/junit-python.xml", "junit"))

    scripts = (package_json or {}).get("scripts") or {}
    test_script = scripts.get("test", "")
    if package_json is not None and test_script and "no test specified" not in test_script:
        deps = {**(package_json.get("dependencies") or {}), **(package_json.get("devDependencies") or {})}
        flags = "" if settings.allow_install_scripts else " --ignore-scripts"
        install = [("npm ci" if "package-lock.json" in files else "npm install") + flags + " --no-audit --no-fund"]
        if "vitest" in deps:
            suites.append(TestSuite("node", install, f"npx vitest run --reporter=junit --outputFile={REPORTS}/junit-node.xml", "junit"))
        elif "jest" in deps:
            suites.append(TestSuite("node", install, f"npx jest --ci --json --outputFile={REPORTS}/jest.json", "jest"))
        else:
            suites.append(TestSuite("node", install, "npm test", None))
    return suites


# ---------------- execution ----------------

class Step:
    """Records a step's status/duration on the scan and in the activity log."""

    def __init__(self, db: AsyncSession, scan: ScanRun, name: str):
        self.db, self.scan, self.name = db, scan, name

    async def __aenter__(self):
        self.t0 = time.monotonic()
        self.scan.steps = {**self.scan.steps, self.name: {"status": "running"}}
        await self.db.commit()
        return self

    async def done(self, status: str, detail: str = "") -> None:
        self.scan.steps = {**self.scan.steps, self.name: {"status": status, "detail": detail[:500],
                                                          "duration_ms": int((time.monotonic() - self.t0) * 1000)}}
        log_event(self.db, SOURCE, f"Scan #{self.scan.id} {self.name}: {status}{' - ' + detail if detail else ''}",
                  level="error" if status == "failed" else "info", type="command", scan_id=self.scan.id, step=self.name)
        await self.db.commit()

    async def __aexit__(self, exc_type, exc, tb):
        if exc is not None and self.scan.steps.get(self.name, {}).get("status") == "running":
            await self.db.rollback()
            await self.db.refresh(self.scan)  # rollback expired it; reload before touching attributes
            await self.done("failed", f"{type(exc).__name__}: {exc}")
        return False


async def _run_tool(sb: Sandbox, cmd: list[str], timeout: int, report: str, ok_codes: set[int]) -> str:
    r = await asyncio.to_thread(sb.exec, cmd, timeout)
    if r.timed_out:
        raise SandboxError(f"timed out after {timeout}s")
    if r.exit_code not in ok_codes:
        raise SandboxError(f"exit {r.exit_code}: {r.output.strip()[-300:]}")
    data = await asyncio.to_thread(sb.read_file, report)
    return (data or b"").decode("utf-8", "replace")


async def execute(db: AsyncSession, scan_id: int, gh: GitHubApp) -> None:
    s = get_settings()
    scan = await db.get(ScanRun, scan_id)
    repo = await db.get(Repository, scan.repository_id)
    inst = await db.get(GitHubInstallation, repo.installation_id)
    cfg = RepoSettings.of(repo.settings)
    scan.status, scan.started_at, scan.steps, scan.summary = "running", datetime.now(UTC), {}, {}
    log_event(db, SOURCE, f"Scan #{scan.id} started for {repo.full_name}", scan_id=scan.id)
    await db.commit()

    findings: list[parsers.Finding] = []
    test_totals = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    source: SourceTree | None = None
    sb: Sandbox | None = None
    try:
        if not (repo.enabled and repo.accessible and not inst.deleted_at and not inst.suspended_at):
            raise SandboxError("repository is not enabled or no longer accessible")

        async with Step(db, scan, "fetch") as st:
            token = await gh.installation_token(inst.installation_id, repository_ids=[repo.github_repo_id],
                                                permissions={"contents": "read", "metadata": "read"})
            cache = RepoCache(s)
            await cache.sync(repo.github_repo_id, repo.full_name, token)  # fetch latest before every task
            scan.commit_sha = await cache.resolve(repo.github_repo_id, repo.default_branch)
            worktree = await cache.add_worktree(repo.github_repo_id, f"scan-{scan.id}", scan.commit_sha)
            archive = await cache.archive(worktree)
            try:
                source = await asyncio.to_thread(repack_tarball, archive, s.max_repo_bytes, s.max_repo_files)
            except tarfile.TarError as e:
                raise SandboxError(f"repository archive is unreadable: {e}") from e
            finally:
                archive.close()
            await st.done("ok", f"fetched {repo.default_branch} @ {scan.commit_sha[:12]}, {len(source.files)} files, {source.total_bytes // 1000} kB")

        async with Step(db, scan, "sandbox") as st:
            sb = Sandbox(s, label=f"scan{scan.id}")
            await asyncio.to_thread(sb.start, source)
            await st.done("ok")

        timeout = cfg.step_timeout_seconds
        tools = [
            ("semgrep", cfg.run_semgrep, ["semgrep", "scan", "--config", "/opt/rules/semgrep-default.yml", "--json",
                                          "--metrics=off", "--disable-version-check", "-q", "--timeout", "30",
                                          "--max-target-bytes", "1000000", "--output", f"{REPORTS}/semgrep.json", "."],
             f"{REPORTS}/semgrep.json", {0, 1}, lambda raw: parsers.parse_semgrep(raw, source.read_lines)),
            ("gitleaks", cfg.run_gitleaks, ["gitleaks", "dir", SRC, "--report-format", "json", "--report-path",
                                            f"{REPORTS}/gitleaks.json", "--no-banner", "--exit-code", "0", "--max-target-megabytes", "5"],
             f"{REPORTS}/gitleaks.json", {0}, parsers.parse_gitleaks),
            # osv-scanner: 0 = clean, 1 = vulnerabilities found, 128 = no package manifests found
            ("osv", cfg.run_osv, ["osv-scanner", "scan", "source", "-r", "--format", "json", "--output", f"{REPORTS}/osv.json", SRC],
             f"{REPORTS}/osv.json", {0, 1, 128}, lambda raw: parsers.parse_osv(raw, SRC)),
        ]
        for name, enabled, cmd, report, ok, parse in tools:
            async with Step(db, scan, name) as st:
                if not enabled:
                    await st.done("skipped", "disabled in repository settings")
                    continue
                try:
                    found = parse(await _run_tool(sb, cmd, timeout, report, ok))
                except (SandboxError, ValueError) as e:  # one tool failing must not hide the others' results
                    await st.done("failed", str(e))
                    continue
                findings += found
                await st.done("ok", f"{len(found)} finding(s)")

        async with Step(db, scan, "tests") as st:
            if not cfg.run_tests:
                await st.done("skipped", "disabled in repository settings")
            else:
                pkg = None
                if "package.json" in source.files:
                    try:
                        pkg = json.loads(source.read_lines("package.json", 1, 100_000) or "{}")
                    except ValueError:
                        pkg = None
                suites = detect_suites(source.files, pkg, cfg)
                if not suites:
                    await st.done("skipped", "no test suite detected; set test_command in repository settings")
                else:
                    details = []
                    for suite in suites:
                        tf, run = await _run_suite(db, sb, scan, suite, timeout)
                        findings += tf
                        for k in test_totals:
                            test_totals[k] += getattr(run, k)
                        details.append(f"{suite.name}: {run.passed} passed, {run.failed} failed, {run.errors} errors, {run.skipped} skipped"
                                       + (" (timed out)" if run.timed_out else ""))
                    await st.done("ok", "; ".join(details))

        async with Step(db, scan, "record") as st:
            stats = await record_findings(db, repo, scan, findings)
            await st.done("ok", f"{stats['new']} new, {stats['seen_again']} already known, {stats['reopened']} reopened")

        failed_steps = [k for k, v in scan.steps.items() if v.get("status") == "failed"]
        by_sev: dict[str, int] = {}
        for f in findings:
            by_sev[f.severity] = by_sev.get(f.severity, 0) + 1
        scan.summary = {"findings": len(findings), "by_severity": by_sev, "tests": test_totals,
                        "failed_steps": failed_steps, **stats}
        scan.status = "partial" if failed_steps else "succeeded"
        log_event(db, SOURCE, f"Scan #{scan.id} {scan.status} for {repo.full_name}: {len(findings)} finding(s), {stats['new']} new",
                  level="warning" if failed_steps else "info", scan_id=scan.id)
        if cfg.auto_create_issues and stats["new"]:
            await _queue_auto_issues(db, repo, scan, cfg)
    except Exception as e:
        expected = isinstance(e, (SandboxError, GitHubError, GitHubNotConfigured, RepoCacheError))
        if not expected:
            log.exception("scan %s crashed", scan_id)
        await db.rollback()
        scan = await db.get(ScanRun, scan_id, populate_existing=True)
        scan.status = "failed"
        scan.error = (str(e) if expected else f"internal error: {type(e).__name__} (see worker logs)")[:2000]
        log_event(db, SOURCE, f"Scan #{scan_id} failed: {scan.error}", level="error", type="error", scan_id=scan_id)
    finally:
        if sb is not None:
            await asyncio.to_thread(sb.destroy)
        if source is not None:
            source.close()
        try:
            await RepoCache(s).remove_worktree(repo.github_repo_id, f"scan-{scan_id}")
        except Exception:
            log.exception("could not remove worktree for scan %s", scan_id)
        scan = await db.get(ScanRun, scan_id, populate_existing=True)
        scan.finished_at = datetime.now(UTC)
        if scan.status == "running":  # unexpected exception escaped
            scan.status, scan.error = "failed", scan.error or "internal error (see worker logs)"
        await db.commit()


async def install_suite(sb: Sandbox, suite: TestSuite, timeout: int) -> list[str]:
    log_lines = []
    for cmd in suite.install:
        r = await asyncio.to_thread(sb.exec, ["sh", "-c", cmd], timeout)
        log_lines.append(f"$ {cmd}\n{r.output[-4000:]}")
        if r.exit_code != 0 and "||" not in cmd:
            log_lines.append(f"[install step failed with exit {r.exit_code}; running tests anyway]")
    return log_lines


async def run_suite_cases(sb: Sandbox, suite: TestSuite, timeout: int):
    """Run one suite's test command; returns (cases, exec result, report format, log lines)."""
    report_name = "junit.xml" if suite.name == "custom" else ("jest.json" if suite.report == "jest" else f"junit-{suite.name}.xml")
    await asyncio.to_thread(sb.exec, ["rm", "-f", f"{REPORTS}/{report_name}"], 30)  # never read a stale report
    r = await asyncio.to_thread(sb.exec, ["sh", "-c", suite.test], timeout)
    log_lines = [f"$ {suite.test}\n{r.output[-20000:]}"]
    cases: list[parsers.TestCase] = []
    fmt = "exit_code"
    try:
        if suite.report in ("junit", "jest"):
            raw = await asyncio.to_thread(sb.read_file, f"{REPORTS}/{report_name}")
            if raw:
                text = raw.decode("utf-8", "replace")
                cases, fmt = (parsers.parse_junit(text), "junit") if suite.report == "junit" else (parsers.parse_jest(text), "jest")
    except (ValueError, SyntaxError) as e:
        log_lines.append(f"[could not parse test report: {e}]")
    return cases, r, fmt, log_lines


async def _run_suite(db: AsyncSession, sb: Sandbox, scan: ScanRun, suite: TestSuite, timeout: int):
    log_lines = await install_suite(sb, suite, timeout)
    cases, r, fmt, run_log = await run_suite_cases(sb, suite, timeout)
    log_lines += run_log


    count = lambda st: sum(1 for c in cases if c.status == st)  # noqa: E731
    run = TestRun(scan_run_id=scan.id, commit_sha=scan.commit_sha, phase="scan", command=suite.test,
                  exit_code=r.exit_code, timed_out=r.timed_out, result_format=fmt,
                  passed=count("passed"), failed=count("failed"), errors=count("error"), skipped=count("skipped"),
                  duration_ms=r.duration_ms, log_tail=parsers.redact("\n".join(log_lines))[-60000:])
    db.add(run)
    await db.flush()
    for c in cases:
        db.add(TestResult(test_run_id=run.id, test_id=c.test_id[:1000], status=c.status,
                          duration_ms=c.duration_ms, message=parsers.redact(c.message)))

    findings = parsers.test_failures_to_findings(cases)
    if not cases and (r.exit_code != 0 or r.timed_out):  # no per-test detail: the suite itself is the finding
        run.errors = 1
        findings.append(parsers.Finding(
            source="auto_test", title=f"Test suite failing ({suite.name}): {suite.test}"[:300],
            description="The test command exited unsuccessfully" + (" (timed out)" if r.timed_out else "") +
                        " and produced no per-test report.",
            severity="medium", fingerprint=parsers.fingerprint("test-suite", suite.name, suite.test),
            evidence=parsers.redact(r.output[-8000:]), evidence_kind="test_output",
            data={"command": suite.test, "exit_code": r.exit_code}))
    await db.commit()
    return findings, run


async def _queue_auto_issues(db: AsyncSession, repo: Repository, scan: ScanRun, cfg: RepoSettings) -> None:
    from sqlalchemy import select
    new = (await db.execute(select(Bug).where(Bug.first_seen_scan_id == scan.id, Bug.github_issue_number.is_(None)))).scalars().all()
    for bug in new:
        if meets_threshold(bug.severity, cfg.auto_issue_min_severity):
            await queue.enqueue(db, "github.create_issue", {"bug_id": bug.id})
