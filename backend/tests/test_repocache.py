"""Repository management rules (persistent clone, fetch-before-task, isolated worktrees, never touch others' work)."""

import os
from pathlib import Path

import pytest

from app.config import get_settings
from app.repocache import RepoCache, RepoCacheError
from tests.git_remote import push_files

RID, NAME = 4242, "realify/rules"


@pytest.fixture
def cache():
    return RepoCache(get_settings())


async def test_clone_once_then_fetch_latest_before_each_task(cache):
    first = push_files(NAME, {"a.py": "v1\n"}, "v1")
    path = await cache.sync(RID, NAME, token="ghs_secret_token_value")
    marker = path / "debugops-marker"
    marker.write_text("persist")  # if the clone were re-created this would vanish
    assert await cache.resolve(RID, "main") == first

    second = push_files(NAME, {"a.py": "v2\n"}, "v2")
    assert await cache.sync(RID, NAME, token="ghs_secret_token_value") == path
    assert marker.exists()  # same persistent clone, not re-cloned
    assert await cache.resolve(RID, "main") == second  # latest base fetched
    config = (path / "config").read_text()
    assert "ghs_secret_token_value" not in config and "extraheader" not in config  # token never stored


async def test_base_branch_only_tracks_github(cache):
    push_files(NAME, {"a.py": "v1\n"})
    path = await cache.sync(RID, NAME, token="t")
    sha = await cache.resolve(RID, "main")
    wt = await cache.add_worktree(RID, "run-7", sha, branch="debugops/run-7")
    (wt / "a.py").write_text("agent edit\n")
    heads = await cache.git("for-each-ref", "--format=%(refname)", "refs/heads", cwd=path)
    assert heads.split() == ["refs/heads/debugops/run-7"]  # no local base branch exists to be modified
    assert await cache.resolve(RID, "main") == sha
    with pytest.raises(RepoCacheError, match="must start with debugops/"):
        await cache.add_worktree(RID, "run-8", sha, branch="main")


async def test_worktree_per_task_and_dirty_worktrees_are_preserved(cache):
    push_files(NAME, {"a.py": "v1\n", "dir/b.py": "b\n"})
    await cache.sync(RID, NAME, token="t")
    sha = await cache.resolve(RID, "main")
    w1 = await cache.add_worktree(RID, "run-1", sha, branch="debugops/run-1")
    w2 = await cache.add_worktree(RID, "scan-2", sha)  # detached: read-only scan
    assert w1 != w2 and (w1 / "a.py").read_text() == "v1\n" and (w2 / "dir/b.py").exists()
    assert await cache.add_worktree(RID, "run-1", sha, branch="debugops/run-1") == w1  # resume reuses it

    archive = await cache.archive(w1)
    import tarfile
    with tarfile.open(fileobj=archive, mode="r:gz") as t:
        assert sorted(m.name for m in t.getmembers() if m.isfile()) == ["src/a.py", "src/dir/b.py"]

    (w2 / "a.py").write_text("someone's uncommitted work\n")
    assert await cache.remove_worktree(RID, "scan-2") is False  # never discard uncommitted changes
    assert (w2 / "a.py").read_text() == "someone's uncommitted work\n"
    with pytest.raises(RepoCacheError, match="unexpected state"):
        await cache.add_worktree(RID, "scan-2", sha)

    assert await cache.remove_worktree(RID, "run-1", delete_branch="debugops/run-1") is True
    assert not w1.exists()
    assert not await cache.git("branch", "--list", "debugops/run-1", cwd=cache.repo_path(RID))


async def test_input_validation(cache):
    with pytest.raises(RepoCacheError):
        cache.worktree_path(RID, "../../etc")
    push_files(NAME, {"a": "1"})
    await cache.sync(RID, NAME, token="t")
    with pytest.raises(RepoCacheError):
        await cache.resolve(RID, "main;rm -rf /")
    with pytest.raises(RepoCacheError, match="fetch failed"):
        await cache.sync(RID + 1, "realify/does-not-exist", token="ghs_leaky")
    try:
        await cache.sync(RID + 1, "realify/does-not-exist", token="ghs_leaky")
    except RepoCacheError as e:
        assert "ghs_leaky" not in str(e)
