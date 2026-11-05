"""Per-run isolated workspace: a sandbox holding the repository as a git repo on a dedicated branch.

Commits inside the sandbox are local bookkeeping only (base -> fix -> tests); nothing is pushed from here.
The original branch on GitHub is never touched by the sandbox.
"""

import asyncio
import re
import shlex
from dataclasses import dataclass, field

from app.config import Settings
from app.detection.scan import TestSuite, install_suite, run_suite_cases
from app.sandbox.runner import SRC, ExecResult, Sandbox, SourceTree

GIT_EXCLUDES = ["node_modules/", ".venv/", "__pycache__/", ".pytest_cache/", "*.pyc", ".mypy_cache/", ".ruff_cache/",
                ".coverage", "coverage/", ".nyc_output/", "dist/", "build/", ".next/", ".tox/"]
_TEST_RE = re.compile(r"(^|/)(tests?|__tests__|spec|specs)/|(^|/)test_[^/]*\.py$|_test\.(py|go)$|"
                      r"\.(test|spec)\.[cm]?[jt]sx?$|(^|/)conftest\.py$")
MAX_FILE_BYTES = 1_000_000


def is_test_path(path: str) -> bool:
    return bool(_TEST_RE.search(path))


class WorkspaceError(Exception):
    pass


@dataclass
class SuiteRun:
    results: dict[str, str] = field(default_factory=dict)  # test_id -> status
    per_test: bool = True  # every suite produced a per-test report
    exit_codes: list[int | None] = field(default_factory=list)
    timed_out: bool = False
    log: str = ""

    def summary(self) -> dict:
        counts: dict[str, int] = {}
        for st in self.results.values():
            counts[st] = counts.get(st, 0) + 1
        return {"total": len(self.results), **counts, "per_test": self.per_test, "exit_codes": self.exit_codes,
                "timed_out": self.timed_out}


class Workspace:
    def __init__(self, run_id: int, sandbox: Sandbox, source: SourceTree, base_sha: str, suites: list[TestSuite],
                 settings: Settings, branch: str):
        self.run_id, self.sb, self.source, self.base_sha = run_id, sandbox, source, base_sha
        self.suites, self.s, self.branch = suites, settings, branch
        self.gateway_token: str | None = None
        self.baseline: SuiteRun | None = None
        self.refs: dict[str, str] = {}  # fix / tests commit SHAs inside this sandbox

    async def sh(self, cmd: str, timeout: int = 300, check: bool = True) -> ExecResult:
        r = await asyncio.to_thread(self.sb.exec, ["sh", "-c", cmd], timeout)
        if check and r.exit_code != 0:
            raise WorkspaceError(f"`{cmd[:80]}` failed ({r.exit_code}): {r.output.strip()[-400:]}")
        return r

    async def git(self, args: str, check: bool = True, timeout: int = 300) -> str:
        r = await self.sh(f"git -c user.name=DebugOps -c user.email=debugops@localhost -c advice.detachedHead=false {args}",
                          timeout, check)
        return r.output.strip()

    async def setup(self) -> list[str]:
        excludes = "\\n".join(GIT_EXCLUDES)
        await self.sh(f"cd {SRC} && git init -q && printf '{excludes}\\n' >> .git/info/exclude && git add -A && "
                      f"git -c user.name=DebugOps -c user.email=debugops@localhost commit -qm base --no-verify --allow-empty "
                      f"&& git tag base && git checkout -qb {shlex.quote(self.branch)}", timeout=600)
        log: list[str] = []
        for suite in self.suites:
            log += await install_suite(self.sb, suite, self.s.agent_step_timeout_seconds)
        return log

    # ---------- git bookkeeping ----------
    async def reset_to(self, ref: str) -> None:
        await self.git(f"checkout -qf -B {shlex.quote(self.branch)} {shlex.quote(ref)}")
        await self.git("clean -fdq")

    async def changed_since(self, ref: str) -> list[tuple[str, str]]:
        """[(status, path)] of working-tree changes vs ref, including untracked files (excludes honoured)."""
        await self.git("add -A")
        out = await self.git(f"diff --cached --name-status --no-renames {shlex.quote(ref)}")
        return [(ln.split("\t", 1)[0][0], ln.split("\t", 1)[1]) for ln in out.splitlines() if "\t" in ln]

    async def revert_paths(self, ref: str, items: list[tuple[str, str]]) -> None:
        for status, path in items:
            q = shlex.quote(path)
            if status == "A":  # file did not exist at ref
                await self.git(f"rm -qf --cached -- {q}", check=False)
                await self.sh(f"rm -f -- {q}", check=False)
            else:
                await self.git(f"checkout {shlex.quote(ref)} -- {q}")

    async def commit(self, message: str) -> str:
        await self.git("add -A")
        await self.git(f"commit -q --no-verify --allow-empty -m {shlex.quote(message)}")
        return await self.git("rev-parse HEAD")

    async def diff(self, a: str, b: str, limit: int = 2_000_000) -> str:
        await self.sh(f"git diff --no-color {shlex.quote(a)} {shlex.quote(b)} > /workspace/reports/.diff")
        data = await asyncio.to_thread(self.sb.read_file, "/workspace/reports/.diff", 50_000_000)
        return (data or b"").decode("utf-8", "replace")[:limit]

    async def files_at(self, a: str, b: str) -> dict[str, str | None]:
        """Changed files between a and b with their content at b (None = deleted)."""
        numstat = await self.git(f"diff --numstat --no-renames {shlex.quote(a)} {shlex.quote(b)}")
        binary = [ln.split("\t", 2)[2] for ln in numstat.splitlines() if ln.startswith("-\t-\t")]
        if binary:
            raise WorkspaceError(f"binary file changes are not supported: {', '.join(binary[:5])}")
        out = await self.git(f"diff --name-status --no-renames {shlex.quote(a)} {shlex.quote(b)}")
        files: dict[str, str | None] = {}
        for ln in out.splitlines():
            status, path = ln.split("\t", 1)
            if status.startswith("D"):
                files[path] = None
                continue
            size = int(await self.git(f"cat-file -s {shlex.quote(b)}:{shlex.quote(path)}"))
            if size > MAX_FILE_BYTES:
                raise WorkspaceError(f"changed file {path} exceeds {MAX_FILE_BYTES} bytes")
            # Via a file on the volume: command output is truncated, file reads are not.
            await self.sh(f"git show {shlex.quote(b)}:{shlex.quote(path)} > /workspace/reports/.blob")
            data = await asyncio.to_thread(self.sb.read_file, "/workspace/reports/.blob", MAX_FILE_BYTES + 1)
            files[path] = (data or b"").decode("utf-8")
        return files

    async def apply_files(self, files: dict[str, str | None]) -> None:
        """Write file contents into the working tree (used to restore a stored attempt after a worker restart)."""
        import io
        import tarfile

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as t:
            for path, content in files.items():
                if content is None:
                    continue
                data = content.encode()
                info = tarfile.TarInfo(f"src/{path}")
                info.size, info.mode, info.uid, info.gid = len(data), 0o644, 10001, 10001
                t.addfile(info, io.BytesIO(data))
        buf.seek(0)
        await asyncio.to_thread(self.sb.container.put_archive, "/workspace", buf)
        for path, content in files.items():
            if content is None:
                await self.sh(f"rm -f -- {shlex.quote(path)}", check=False)

    # ---------- tests ----------
    async def run_tests(self) -> SuiteRun:
        run = SuiteRun()
        logs = []
        for suite in self.suites:
            cases, r, fmt, log = await run_suite_cases(self.sb, suite, self.s.agent_step_timeout_seconds)
            run.exit_codes.append(r.exit_code)
            run.timed_out |= r.timed_out
            run.per_test &= fmt != "exit_code" and bool(cases)
            for c in cases:
                run.results[c.test_id] = c.status
            logs += log
        run.per_test &= bool(self.suites)
        run.log = "\n".join(logs)[-30000:]
        return run

    async def run_tests_at(self, ref: str, overlay_from: str | None = None, overlay_paths: list[str] = ()) -> SuiteRun:
        """Check out `ref` (detached), optionally overlay some paths from another commit, run the suites."""
        await self.git(f"checkout -qf --detach {shlex.quote(ref)}")
        await self.git("clean -fdq")
        if overlay_from and overlay_paths:
            await self.git(f"checkout {shlex.quote(overlay_from)} -- " + " ".join(shlex.quote(p) for p in overlay_paths))
        try:
            return await self.run_tests()
        finally:
            await self.git("checkout -qf .")
            await self.git("clean -fdq")

    def destroy(self) -> None:
        self.sb.destroy()
        self.source.close()


# Live workspaces in this worker process. Rebuilt from the database if a run resumes in another process.
REGISTRY: dict[int, Workspace] = {}

