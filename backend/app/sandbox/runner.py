"""Ephemeral, credential-free sandbox containers for running scanners and repository tests.

Isolation per sandbox: non-root uid 10001, read-only root FS, all capabilities dropped, no-new-privileges,
memory/CPU/PID limits, per-command timeouts plus a container-wide hard deadline, and a network that can
only reach an allowlisting egress proxy (or no network at all). Source code is streamed in by the worker,
so GitHub tokens never enter the sandbox.
"""

import io
import logging
import tarfile
import tempfile
import time
import uuid
from dataclasses import dataclass
from typing import IO

import docker
from docker.errors import APIError, NotFound

from app.config import Settings

log = logging.getLogger("debugops.sandbox")

SANDBOX_UID = 10001
LABEL = "debugops.sandbox"
SRC = "/workspace/src"
REPORTS = "/workspace/reports"
MAX_OUTPUT = 200_000  # bytes of command output kept (tail)


class SandboxError(Exception):
    pass


class RepoTooLarge(SandboxError):
    pass


@dataclass
class ExecResult:
    exit_code: int | None
    output: str
    timed_out: bool
    duration_ms: int


# ---------------- source preparation ----------------

class SourceTree:
    """Repacked source tar (uid 10001, rooted at src/) kept on local disk for snippet lookups."""

    def __init__(self, spool: IO[bytes], files: set[str], total_bytes: int):
        self.spool = spool
        self.files = files
        self.total_bytes = total_bytes
        self._tar: tarfile.TarFile | None = None

    def tar_bytes(self) -> IO[bytes]:
        self.spool.seek(0)
        return self.spool

    def read_lines(self, path: str, start: int, end: int) -> str:
        if path not in self.files:
            return ""
        try:
            if self._tar is None:
                self.spool.seek(0)
                self._tar = tarfile.open(fileobj=self.spool, mode="r:")
            f = self._tar.extractfile(f"src/{path}")
            if f is None:
                return ""
            text = f.read(2_000_000).decode("utf-8", "replace").splitlines()
            return "\n".join(text[max(0, start - 1):max(start, end)])
        except (KeyError, tarfile.TarError):
            return ""

    def close(self):
        self.spool.close()


def repack_tarball(gz: IO[bytes], max_bytes: int, max_files: int) -> SourceTree:
    """GitHub tarball (top dir `owner-repo-sha/`) -> plain tar rooted at `src/`, owned by the sandbox user.

    Drops device files, FIFOs and hard links; rejects absolute or `..` member names; enforces size limits.
    """
    out = tempfile.SpooledTemporaryFile(max_size=64_000_000)
    files: set[str] = set()
    dirs: set[str] = set()
    total = 0
    with tarfile.open(fileobj=gz, mode="r|gz") as src, tarfile.open(fileobj=out, mode="w:") as dst:
        for d in ("src", "reports"):  # explicit entries so Docker does not create them root-owned
            info = tarfile.TarInfo(d)
            info.type, info.mode, info.uid, info.gid = tarfile.DIRTYPE, 0o755, SANDBOX_UID, SANDBOX_UID
            dst.addfile(info)
        for m in src:
            parts = m.name.split("/", 1)
            if len(parts) < 2 or not parts[1]:
                continue  # the top-level directory itself / pax headers
            rel = parts[1].rstrip("/")
            if rel.startswith("/") or ".." in rel.split("/"):
                raise SandboxError(f"unsafe path in repository archive: {rel!r}")
            if not (m.isfile() or m.isdir() or m.issym()):
                continue
            if m.isfile():
                total += m.size
                if total > max_bytes:
                    raise RepoTooLarge(f"repository exceeds {max_bytes // 1_000_000} MB limit")
            if len(files) >= max_files:
                raise RepoTooLarge(f"repository exceeds {max_files} files limit")
            parents = rel.split("/")[:-1]
            for i in range(1, len(parents) + 1):  # archives may omit directory entries; never let Docker create them root-owned
                d = "/".join(parents[:i])
                if d not in dirs:
                    dirs.add(d)
                    di = tarfile.TarInfo(f"src/{d}")
                    di.type, di.mode, di.uid, di.gid, di.uname, di.gname = tarfile.DIRTYPE, 0o755, SANDBOX_UID, SANDBOX_UID, "sandbox", "sandbox"
                    dst.addfile(di)
            if m.isdir():
                if rel in dirs:
                    continue
                dirs.add(rel)
            info = tarfile.TarInfo(f"src/{rel}")
            info.type, info.size, info.mtime = m.type, m.size if m.isfile() else 0, m.mtime
            info.linkname = m.linkname if m.issym() else ""
            info.mode = 0o755 if m.isdir() else (m.mode & 0o755) | 0o600
            info.uid = info.gid = SANDBOX_UID
            info.uname = info.gname = "sandbox"
            dst.addfile(info, src.extractfile(m) if m.isfile() else None)
            if m.isfile():
                files.add(rel)
    return SourceTree(out, files, total)


# ---------------- container lifecycle ----------------

class Sandbox:
    def __init__(self, settings: Settings, client: docker.DockerClient | None = None, label: str | None = None):
        self.s = settings
        self.client = client or docker.from_env()
        self.name = f"debugops-sbx-{label or uuid.uuid4().hex[:12]}"
        self.container = None
        self.volume = None

    def start(self, source: SourceTree) -> None:
        s = self.s
        self.volume = self.client.volumes.create(name=self.name, labels={LABEL: "1"})
        env = {"HOME": "/tmp", "npm_config_cache": "/tmp/.npm", "PIP_CACHE_DIR": "/tmp/.pip", "CI": "true",
               "PYTHONDONTWRITEBYTECODE": "1"}
        if s.sandbox_proxy_url and s.sandbox_network != "none":
            env.update({"HTTP_PROXY": s.sandbox_proxy_url, "HTTPS_PROXY": s.sandbox_proxy_url,
                        "http_proxy": s.sandbox_proxy_url, "https_proxy": s.sandbox_proxy_url,
                        "NO_PROXY": "localhost,127.0.0.1", "no_proxy": "localhost,127.0.0.1"})
        net = {"network_mode": "none"} if s.sandbox_network == "none" else {"network": s.sandbox_network}
        self.container = self.client.containers.create(
            s.sandbox_image,
            command=["sleep", str(s.sandbox_scan_timeout_seconds)],  # hard deadline: container exits by itself
            name=self.name, user=f"{SANDBOX_UID}:{SANDBOX_UID}", working_dir=SRC, environment=env,
            mem_limit=s.sandbox_memory, memswap_limit=s.sandbox_memory, nano_cpus=int(s.sandbox_cpus * 1e9),
            pids_limit=s.sandbox_pids, read_only=True, cap_drop=["ALL"], security_opt=["no-new-privileges"],
            tmpfs={"/tmp": f"rw,exec,nosuid,nodev,size={s.sandbox_tmpfs_size}"},
            volumes={self.name: {"bind": "/workspace", "mode": "rw"}},
            labels={LABEL: "1", "debugops.created": str(int(time.time()))}, init=True, **net,
        )
        if not self.container.put_archive("/workspace", source.tar_bytes()):
            raise SandboxError("failed to copy source into sandbox")
        self.container.start()

    def exec(self, cmd: list[str], timeout: int, workdir: str = SRC, env: dict | None = None) -> ExecResult:
        assert self.container is not None
        started = time.monotonic()
        api = self.client.api
        # `timeout -s KILL` inside the container: the command is killed even if the worker dies.
        eid = api.exec_create(self.container.id, ["timeout", "-s", "KILL", str(timeout), *cmd],
                              user=f"{SANDBOX_UID}:{SANDBOX_UID}", workdir=workdir, environment=env or {})["Id"]
        buf = bytearray()
        for chunk in api.exec_start(eid, stream=True):
            buf.extend(chunk)
            if len(buf) > MAX_OUTPUT * 2:
                del buf[: len(buf) - MAX_OUTPUT]
        code = api.exec_inspect(eid).get("ExitCode")
        out = bytes(buf[-MAX_OUTPUT:]).decode("utf-8", "replace")
        return ExecResult(exit_code=code, output=out, timed_out=code == 137,
                          duration_ms=int((time.monotonic() - started) * 1000))

    def exec_stream(self, cmd: list[str], timeout: int, on_line, workdir: str = SRC, env: dict | None = None) -> ExecResult:
        """Like exec(), but calls on_line(str) for every output line as it is produced (live agent activity)."""
        assert self.container is not None
        started = time.monotonic()
        api = self.client.api
        eid = api.exec_create(self.container.id, ["timeout", "-s", "KILL", str(timeout), *cmd],
                              user=f"{SANDBOX_UID}:{SANDBOX_UID}", workdir=workdir, environment=env or {})["Id"]
        buf, pending = bytearray(), b""
        for chunk in api.exec_start(eid, stream=True):
            buf.extend(chunk)
            if len(buf) > MAX_OUTPUT * 2:
                del buf[: len(buf) - MAX_OUTPUT]
            pending += chunk
            *lines, pending = pending.split(b"\n")
            for ln in lines:
                try:
                    on_line(ln.decode("utf-8", "replace"))
                except Exception:
                    log.exception("on_line callback failed")
            if len(pending) > 1_000_000:  # pathological single line: drop it from the live view
                pending = b""
        if pending:
            on_line(pending.decode("utf-8", "replace"))
        code = api.exec_inspect(eid).get("ExitCode")
        return ExecResult(exit_code=code, output=bytes(buf[-MAX_OUTPUT:]).decode("utf-8", "replace"), timed_out=code == 137,
                          duration_ms=int((time.monotonic() - started) * 1000))

    def read_file(self, path: str, max_bytes: int = 50_000_000) -> bytes | None:
        """Read a file the sandbox produced (must live on the /workspace volume, not tmpfs)."""
        assert self.container is not None
        try:
            stream, stat = self.container.get_archive(path)
        except NotFound:
            return None
        if stat.get("size", 0) > max_bytes:
            raise SandboxError(f"{path} exceeds {max_bytes} bytes")
        data = b"".join(stream)
        with tarfile.open(fileobj=io.BytesIO(data)) as t:
            m = t.next()
            f = t.extractfile(m) if m else None
            return f.read() if f else None

    def destroy(self) -> None:
        for obj in (self.container, self.volume):
            if obj is None:
                continue
            try:
                obj.remove(force=True)
            except NotFound:
                pass
            except APIError:
                log.exception("failed to remove %s", self.name)


def cleanup_orphans(settings: Settings, client: docker.DockerClient | None = None, older_than: int = 3600) -> int:
    """Remove sandbox containers/volumes left behind by a crashed worker."""
    client = client or docker.from_env()
    cutoff, removed = time.time() - older_than, 0
    for c in client.containers.list(all=True, filters={"label": LABEL}):
        if int(c.labels.get("debugops.created", "0")) < cutoff:
            c.remove(force=True)
            removed += 1
    for v in client.volumes.list(filters={"label": LABEL}):
        try:
            v.remove()
        except APIError:
            pass  # still in use by a live sandbox
    return removed
