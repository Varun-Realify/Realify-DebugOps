from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError

from app.auth import CSRF_HEADER, SESSION_COOKIE
from app.models import AuditLog, Role, Session, User
from tests.conftest import PASSWORD, login, make_user, new_client


async def test_login_sets_secure_cookies_and_me_works(db):
    u = await make_user(Role.developer)
    async with new_client() as c:
        r = await login(c, "DEVELOPER@example.com")  # email is case-insensitive
        assert r.status_code == 200 and r.json()["role"] == "developer"
        set_cookie = ",".join(r.headers.get_list("set-cookie"))
        assert "debugops_session=" in set_cookie and "HttpOnly" in set_cookie and "SameSite=lax" in set_cookie
        assert (await c.get("/api/v1/auth/me")).json()["email"] == u.email
        token = c.cookies[SESSION_COOKIE]
    stored = (await db.execute(select(Session.token_hash))).scalar_one()
    assert stored != token and len(stored) == 64  # only the hash is stored


async def test_failures_are_indistinguishable():
    await make_user(Role.developer)
    async with new_client() as c:
        wrong = await login(c, "developer@example.com", "wrong-password-123")
        unknown = await login(c, "nobody@example.com", "wrong-password-123")
        odd = await login(c, "admin@corp.local", "wrong-password-123")  # never 422: login is lookup-only
    assert wrong.status_code == unknown.status_code == odd.status_code == 401
    assert wrong.json() == unknown.json() == odd.json()


async def test_lockout_after_repeated_failures(db):
    await make_user(Role.developer)
    async with new_client() as c:
        for _ in range(5):
            assert (await login(c, "developer@example.com", "wrong-password-123")).status_code == 401
        assert (await login(c, "developer@example.com")).status_code == 401  # correct password, still locked
    actions = (await db.execute(select(AuditLog.action))).scalars().all()
    assert "auth.lockout" in actions

    await db.execute(update(User).values(locked_until=datetime.now(UTC) - timedelta(seconds=1)))
    await db.commit()
    async with new_client() as c:
        assert (await login(c, "developer@example.com")).status_code == 200


async def test_mutation_requires_csrf_token():
    await make_user(Role.developer)
    async with new_client() as c:
        await login(c, "developer@example.com")
        token = c.headers.pop(CSRF_HEADER)
        assert (await c.post("/api/v1/auth/logout")).status_code == 403
        c.headers[CSRF_HEADER] = "0" * 64
        assert (await c.post("/api/v1/auth/logout")).status_code == 403
        c.headers[CSRF_HEADER] = token
        assert (await c.post("/api/v1/auth/logout")).status_code == 204


async def test_cross_origin_mutation_rejected():
    await make_user(Role.developer)
    async with new_client() as c:
        await login(c, "developer@example.com")
        r = await c.post("/api/v1/auth/logout", headers={"Origin": "https://evil.example"})
        assert r.status_code == 403


async def test_logout_revokes_session():
    await make_user(Role.developer)
    async with new_client() as c:
        await login(c, "developer@example.com")
        token = c.cookies[SESSION_COOKIE]
        assert (await c.post("/api/v1/auth/logout")).status_code == 204
    async with new_client() as replay:
        replay.cookies.set(SESSION_COOKIE, token)
        assert (await replay.get("/api/v1/auth/me")).status_code == 401


async def test_idle_and_absolute_expiry(db):
    await make_user(Role.developer)
    async with new_client() as c:
        await login(c, "developer@example.com")
        await db.execute(update(Session).values(last_seen_at=datetime.now(UTC) - timedelta(hours=3)))
        await db.commit()
        assert (await c.get("/api/v1/auth/me")).status_code == 401
    async with new_client() as c:
        await login(c, "developer@example.com")
        await db.execute(update(Session).where(Session.revoked_at.is_(None)).values(expires_at=datetime.now(UTC)))
        await db.commit()
        assert (await c.get("/api/v1/auth/me")).status_code == 401


async def test_deactivated_user_session_rejected(db):
    await make_user(Role.developer)
    async with new_client() as c:
        await login(c, "developer@example.com")
        await db.execute(update(User).values(is_active=False))
        await db.commit()
        assert (await c.get("/api/v1/auth/me")).status_code == 401


async def test_change_password_revokes_other_sessions():
    await make_user(Role.developer)
    async with new_client() as a, new_client() as b:
        await login(a, "developer@example.com")
        await login(b, "developer@example.com")
        r = await a.post("/api/v1/auth/change-password", json={"current_password": PASSWORD, "new_password": "short"})
        assert r.status_code == 422
        r = await a.post("/api/v1/auth/change-password",
                         json={"current_password": PASSWORD, "new_password": "a-much-better-passphrase"})
        assert r.status_code == 204
        assert (await a.get("/api/v1/auth/me")).status_code == 200
        assert (await b.get("/api/v1/auth/me")).status_code == 401
    async with new_client() as c:
        assert (await login(c, "developer@example.com", "a-much-better-passphrase")).status_code == 200


async def test_audit_log_is_append_only(db):
    await make_user(Role.developer)
    async with new_client() as c:
        await login(c, "developer@example.com")
    with pytest.raises(DBAPIError, match="append-only"):
        await db.execute(text("UPDATE audit_logs SET action = 'tampered'"))
    await db.rollback()
    with pytest.raises(DBAPIError, match="append-only"):
        await db.execute(text("DELETE FROM audit_logs"))
