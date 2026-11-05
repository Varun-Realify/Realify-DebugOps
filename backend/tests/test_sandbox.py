"""Sandbox isolation and resource limits, verified against real Docker."""

import io
import tarfile

import pytest

from app.config import get_settings
from app.sandbox.runner import RepoTooLarge, Sandbox, SandboxError, cleanup_orphans, repack_tarball
from tests.test_detection import _sandbox_image_available, gz_repo

pytestmark = pytest.mark.skipif(not _sandbox_image_available(), reason="Docker or the debugops-sandbox image is not available")


@pytest.fixture
def sandbox(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "sandbox_network", "none")
    monkeypatch.setattr(s, "sandbox_memory", "256m")
    monkeypatch.setattr(s, "sandbox_pids", 64)
    sb = Sandbox(s, label="limits-test")
    sb.start(repack_tarball(io.BytesIO(gz_repo({"a.txt": "hi"})), s.max_repo_bytes, s.max_repo_files))
    yield sb
    sb.destroy()


def test_isolation_properties(sandbox):
    out = sandbox.exec(["sh", "-c", "id -u; grep CapEff /proc/self/status; touch /etc/x 2>&1; "
                                    "python3 -c 'import socket; socket.gethostbyname(\"pypi.org\")' 2>&1 | tail -1; env"], 30).output
    assert out.splitlines()[0] == "10001"  # non-root
    assert "CapEff:\t0000000000000000" in out  # no capabilities
    assert "Read-only file system" in out
    assert "Temporary failure in name resolution" in out or "Name or service not known" in out  # no network
    for secret in ("ANTHROPIC", "GITHUB", "DATABASE_URL", "SECRET_KEY"):
        assert secret not in out  # no credentials or config in the environment


def test_memory_limit_kills_runaway_process(sandbox):
    r = sandbox.exec(["python3", "-c", "x = bytearray(1024 * 1024 * 1024)"], 60)
    assert r.exit_code in (137, 1) and ("MemoryError" in r.output or r.exit_code == 137)
    assert sandbox.exec(["true"], 10).exit_code == 0  # container still usable


def test_pid_limit_contains_fork_bomb(sandbox):
    r = sandbox.exec(["python3", "-c", "import os, time\nfor _ in range(500):\n    if os.fork() == 0:\n        time.sleep(30); os._exit(0)"], 20)
    assert r.exit_code != 0 and ("Resource temporarily unavailable" in r.output or "BlockingIOError" in r.output or r.timed_out)


def test_command_timeout(sandbox):
    r = sandbox.exec(["sleep", "30"], 2)
    assert r.timed_out and r.exit_code == 137


def test_orphan_cleanup():
    s = get_settings()
    sb = Sandbox(s, label="orphan-test")
    sb.start(repack_tarball(io.BytesIO(gz_repo({"a.txt": "hi"})), s.max_repo_bytes, s.max_repo_files))
    assert cleanup_orphans(s, older_than=-10) >= 1  # treated as left behind by a dead worker
    sb.container = sb.volume = None


def test_repack_rejects_unsafe_and_oversized_archives():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        info = tarfile.TarInfo("top/../../etc/passwd")
        info.size = 1
        t.addfile(info, io.BytesIO(b"x"))
    buf.seek(0)
    with pytest.raises(SandboxError, match="unsafe path"):
        repack_tarball(buf, 10**6, 100)
    with pytest.raises(RepoTooLarge):
        repack_tarball(io.BytesIO(gz_repo({"big.txt": "x" * 5000})), 1000, 100)
    with pytest.raises(RepoTooLarge):
        repack_tarball(io.BytesIO(gz_repo({f"f{i}.txt": "x" for i in range(20)})), 10**6, 5)
