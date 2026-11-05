"""Real local git remotes standing in for GitHub repositories (fetched over file://)."""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

REMOTES = Path(os.environ["GIT_REMOTE_BASE"].removeprefix("file://"))
ENV = {**os.environ, "GIT_AUTHOR_NAME": "dev", "GIT_AUTHOR_EMAIL": "dev@example.com", "GIT_COMMITTER_NAME": "dev",
       "GIT_COMMITTER_EMAIL": "dev@example.com", "GIT_CONFIG_GLOBAL": "/dev/null"}


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, env=ENV, check=True, capture_output=True)


def push_files(full_name: str, files: dict[str, str], message: str = "update", branch: str = "main") -> str:
    """Commit `files` (full tree) to `branch` of the remote, creating the remote if needed. Returns the commit sha."""
    bare = REMOTES / f"{full_name}.git"
    if not bare.exists():
        bare.parent.mkdir(parents=True, exist_ok=True)
        _git("init", "--bare", "--quiet", "--initial-branch", branch, str(bare), cwd=REMOTES)
    work = Path(tempfile.mkdtemp())
    try:
        _git("init", "--quiet", "--initial-branch", branch, cwd=work)
        for path, content in files.items():
            p = work / path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        _git("add", "-A", cwd=work)
        _git("commit", "--quiet", "--allow-empty", "-m", message, cwd=work)
        _git("push", "--quiet", "--force", str(bare), f"HEAD:refs/heads/{branch}", cwd=work)
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=work, env=ENV, capture_output=True, text=True).stdout.strip()
    finally:
        shutil.rmtree(work, ignore_errors=True)


def reset():
    shutil.rmtree(REMOTES, ignore_errors=True)
    REMOTES.mkdir(parents=True, exist_ok=True)
