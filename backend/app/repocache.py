"""Persistent local clone per repository + one isolated git worktree per task.

Repository management rules implemented here:
1-2. One persistent bare clone per repository (`<cache>/<github_repo_id>.git`), created once, never re-cloned.
3.   `sync()` fetches the latest code from GitHub before every scan / debugging run.
4.   The base branch is only ever updated by fetching from GitHub (refs/remotes/origin/*); nothing commits to it.
5-6. Each task gets its own worktree (debugging runs: branch `debugops/run-N`; scans: detached). The sandbox
     receives that worktree's content; all code changes happen inside the sandbox, never in the shared clone.
9.   A worktree is only removed when it has no uncommitted changes; otherwise it is left for a human.

Security: the clone lives on the worker's volume and is never mounted into a sandbox (untrusted code could
otherwise plant git hooks). Every git command runs with hooks disabled, no system config and no prompts; the
GitHub token is passed per command (http.extraheader), never written to git config.
"""

import asyncio
import base64
import fcntl
import logging
import os
import re
import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import IO

from app.config import Settings

log = logging.getLogger("debugops.repocache")
TASK_BRANCH_PREFIX = "debugops/"
_REF_OK = re.compile(r"^[A-Za-z0-9._/\-]{1,255}$")


class RepoCacheError(Exception):
    pass


class RepoCache:
    def __init__(self, settings: Settings):
        self.s = settings
        self.root = Path(settings.repo_cache_dir)
        self.wt_root = self.root / "worktrees"

    # ---------- helpers ----------
    def repo_path(self, github_repo_id: int) -> Path:
        return self.root / f"{int(github_repo_id)}.git"

    def remote_url(self, full_name: str) -> str:
        base = (self.s.git_remote_base or self.s.github_web_url).rstrip("/")
        return f"{base}/{full_name}.git"

    async def git(self, *args: str, cwd: Path | None = None, token: str | None = None, timeout: int = 1800,
                  stdout_file: IO[bytes] | None = None) -> str:
        """Run git with an argv list (no shell). Hooks off, no system/global config, never prompts."""
        cfg = ["-c", "core.hooksPath=/dev/null", "-c", "protocol.ext.allow=never", "-c", "core.fsmonitor=false",
               "-c", "submodule.recurse=false", "-c", "maintenance.auto=false", "-c", "gc.auto=0"]
        if token:
            basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
            cfg += ["-c", f"http.extraheader=AUTHORIZATION: basic {basic}"]
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": tempfile.gettempdir(), "GIT_TERMINAL_PROMPT": "0",
               "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "LC_ALL": "C"}
        proc = await asyncio.create_subprocess_exec(
            "git", *cfg, *args, cwd=str(cwd) if cwd else None, env=env,
            stdout=stdout_file.fileno() if stdout_file else asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except TimeoutError:
            proc.kill()
            raise RepoCacheError(f"git {args[0]} timed out after {timeout}s") from None
        if proc.returncode != 0:
            msg = (err or b"").decode("utf-8", "replace").strip()
            if token:
                msg = msg.replace(token, "****")
            raise RepoCacheError(f"git {args[0]} failed: {msg[-500:]}")
        return (out or b"").decode("utf-8", "replace").strip()

    @asynccontextmanager
    async def _lock(self, github_repo_id: int):
        """Cross-process lock (workers share the cache volume) around fetch / worktree changes."""
        self.root.mkdir(parents=True, exist_ok=True)
        f = open(self.root / f"{int(github_repo_id)}.lock", "w")
        try:
            await asyncio.to_thread(fcntl.flock, f, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)
            f.close()

    # ---------- persistent clone ----------
    async def sync(self, github_repo_id: int, full_name: str, token: str) -> Path:
        """Create the persistent clone if missing (once), then fetch the latest code from GitHub."""
        path = self.repo_path(github_repo_id)
        async with self._lock(github_repo_id):
            if not (path / "HEAD").exists():
                path.mkdir(parents=True, exist_ok=True)
                await self.git("init", "--bare", "--quiet", str(path))
                log.info("created persistent clone for %s at %s", full_name, path)
            url = self.remote_url(full_name)  # re-set every time: follows repository renames/transfers
            if "origin" in (await self.git("remote", cwd=path)).split():
                await self.git("remote", "set-url", "origin", url, cwd=path)
            else:
                await self.git("remote", "add", "origin", url, cwd=path)
            # Base branches live only under refs/remotes/origin/* and are only ever updated from GitHub.
            await self.git("fetch", "--prune", "--no-tags", "--quiet", "origin", "+refs/heads/*:refs/remotes/origin/*",
                           cwd=path, token=token)
            await self.git("worktree", "prune", cwd=path)
        return path

    async def resolve(self, github_repo_id: int, branch: str) -> str:
        if not _REF_OK.match(branch) or ".." in branch:
            raise RepoCacheError(f"invalid branch name {branch!r}")
        return await self.git("rev-parse", "--verify", f"refs/remotes/origin/{branch}^{{commit}}", cwd=self.repo_path(github_repo_id))

    # ---------- per-task worktrees ----------
    def worktree_path(self, github_repo_id: int, task: str) -> Path:
        if not re.fullmatch(r"(run|scan)-\d+", task):
            raise RepoCacheError(f"invalid task name {task!r}")
        return self.wt_root / str(int(github_repo_id)) / task

    async def add_worktree(self, github_repo_id: int, task: str, sha: str, branch: str | None = None) -> Path:
        """Isolated worktree for one task at `sha`. With `branch` (must be debugops/...), on that new branch."""
        repo, wt = self.repo_path(github_repo_id), self.worktree_path(github_repo_id, task)
        if branch is not None and not branch.startswith(TASK_BRANCH_PREFIX):
            raise RepoCacheError(f"task branches must start with {TASK_BRANCH_PREFIX}")
        async with self._lock(github_repo_id):
            if wt.exists():  # resumed task: reuse only if it is exactly what we expect
                head = await self.git("rev-parse", "HEAD", cwd=wt)
                if head != sha or await self.git("status", "--porcelain", cwd=wt):
                    raise RepoCacheError(f"worktree {wt} exists with unexpected state; left untouched for review")
                return wt
            wt.parent.mkdir(parents=True, exist_ok=True)
            if branch is None:
                await self.git("worktree", "add", "--quiet", "--detach", str(wt), sha, cwd=repo)
            else:
                exists = await self.git("branch", "--list", branch, cwd=repo)
                if exists:  # same task resuming after a crash: the branch must still point at the base commit
                    if await self.git("rev-parse", f"refs/heads/{branch}", cwd=repo) != sha:
                        raise RepoCacheError(f"branch {branch} already exists at a different commit; not reusing it")
                    await self.git("worktree", "add", "--quiet", str(wt), branch, cwd=repo)
                else:
                    await self.git("worktree", "add", "--quiet", "-b", branch, str(wt), sha, cwd=repo)
        return wt

    async def archive(self, worktree: Path) -> IO[bytes]:
        """The worktree's committed content as a .tar.gz with one top-level directory (what the sandbox ingests)."""
        out = tempfile.SpooledTemporaryFile(max_size=64_000_000)
        await self.git("archive", "--format=tar.gz", "--prefix=src/", "HEAD", cwd=worktree, stdout_file=out)
        out.seek(0)
        return out

    async def remove_worktree(self, github_repo_id: int, task: str, *, delete_branch: str | None = None) -> bool:
        """Remove a task worktree. Never discards uncommitted changes: a dirty worktree is left in place."""
        repo, wt = self.repo_path(github_repo_id), self.worktree_path(github_repo_id, task)
        async with self._lock(github_repo_id):
            if wt.exists():
                if await self.git("status", "--porcelain", cwd=wt):
                    log.warning("worktree %s has uncommitted changes; leaving it for a human", wt)
                    return False
                await self.git("worktree", "remove", str(wt), cwd=repo)
                shutil.rmtree(wt, ignore_errors=True)
            if delete_branch and delete_branch.startswith(TASK_BRANCH_PREFIX) and await self.git("branch", "--list", delete_branch, cwd=repo):
                await self.git("branch", "-D", delete_branch, cwd=repo)
        return True
