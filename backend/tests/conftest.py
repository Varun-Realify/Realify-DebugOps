"""Integration tests run against a real PostgreSQL (TEST_DATABASE_URL), migrated with Alembic."""

import os
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path

os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://debugops:debugops@127.0.0.1:55432/debugops"
)
os.environ["SECRET_KEY"] = "test-secret-key-" + "x" * 32
os.environ["COOKIE_SECURE"] = "false"  # test client talks plain http
os.environ["LOG_FORMAT"] = "text"

from tests.fake_github import API, PRIVATE_PEM  # noqa: E402
import tempfile as _tempfile  # noqa: E402

_TMP = _tempfile.mkdtemp(prefix="debugops-test-")
os.environ["REPO_CACHE_DIR"] = f"{_TMP}/cache"
os.environ["GIT_REMOTE_BASE"] = f"file://{_TMP}/remotes"

os.environ.update({
    "GITHUB_APP_ID": "12345",
    "GITHUB_APP_SLUG": "debugops-test",
    "GITHUB_APP_PRIVATE_KEY": PRIVATE_PEM.replace("\n", "\\n"),  # exercise the escaped-newline form
    "GITHUB_WEBHOOK_SECRET": "webhook-test-secret",
    "GITHUB_API_URL": API,
    "ANTHROPIC_API_KEY": "test-anthropic-key",
})

import httpx  # noqa: E402
import pytest  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.auth import CSRF_COOKIE, CSRF_HEADER, hash_password  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Role, User  # noqa: E402

PASSWORD = "correct-horse-battery"


@pytest.fixture(scope="session", autouse=True)
def migrate():
    subprocess.run(["alembic", "upgrade", "head"], cwd=Path(__file__).parent.parent, check=True, capture_output=True)


@pytest.fixture(autouse=True)
def reset_login_throttle():
    from app.api import auth as auth_api
    auth_api._attempts.clear()


@pytest.fixture(autouse=True)
def clean_git():
    import shutil
    from tests import git_remote
    shutil.rmtree(os.environ["REPO_CACHE_DIR"], ignore_errors=True)
    git_remote.reset()


@pytest.fixture(autouse=True)
async def clean_db():
    async with SessionLocal() as db:
        # TRUNCATE does not fire the append-only row trigger on audit_logs.
        exists = await db.scalar(text("SELECT to_regclass('checkpoints') IS NOT NULL"))
        if exists:  # LangGraph's own tables
            await db.execute(text("TRUNCATE checkpoints, checkpoint_blobs, checkpoint_writes"))
        await db.execute(text("TRUNCATE users, sessions, audit_logs, jobs, activity_events, workers, webhook_deliveries, repositories, github_installations, scan_runs, bugs, evidence, test_runs, test_results, workflow_runs, agent_steps, fix_attempts, approvals, pull_requests, reports, gateway_tokens RESTART IDENTITY CASCADE"))
        await db.commit()
    yield


@pytest.fixture
async def db():
    async with SessionLocal() as s:
        yield s


async def make_user(role: Role = Role.developer, email: str | None = None, password: str = PASSWORD, **kw) -> User:
    async with SessionLocal() as db:
        u = User(email=email or f"{role.value}@example.com", role=role, password_hash=hash_password(password), **kw)
        db.add(u)
        await db.commit()
        return u


def new_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def login(client: httpx.AsyncClient, email: str, password: str = PASSWORD) -> httpx.Response:
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    if r.status_code == 200:
        client.headers[CSRF_HEADER] = client.cookies[CSRF_COOKIE]
    return r


@asynccontextmanager
async def logged_in(role: Role):
    u = await make_user(role)
    async with new_client() as c:
        assert (await login(c, u.email)).status_code == 200
        yield c
