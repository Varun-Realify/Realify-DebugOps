"""Authentication: password hashing, server-side sessions, CSRF, RBAC dependencies."""

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Depends, HTTPException, Request, Response, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_db
from app.models import Role, Session, User

SESSION_COOKIE = "debugops_session"
CSRF_COOKIE = "debugops_csrf"
CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
MIN_PASSWORD_LEN = 12

_ph = PasswordHasher()
_DUMMY_HASH = _ph.hash("timing-equalizer-not-a-password")


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    """Constant-ish time: always runs one argon2 verify, even for unknown users."""
    try:
        return _ph.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerificationError, InvalidHashError):
        return False


def validate_password_strength(password: str) -> None:
    if len(password) < MIN_PASSWORD_LEN:
        raise HTTPException(422, f"Password must be at least {MIN_PASSWORD_LEN} characters")


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def csrf_for(session_token: str) -> str:
    """CSRF token bound to the session; nothing extra to store."""
    return hmac.new(get_settings().secret_key.encode(), session_token.encode(), hashlib.sha256).hexdigest()


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


async def create_session(db: AsyncSession, user: User, request: Request, response: Response) -> None:
    s = get_settings()
    token = secrets.token_urlsafe(32)
    db.add(
        Session(
            token_hash=_hash_token(token),
            user_id=user.id,
            expires_at=datetime.now(UTC) + timedelta(hours=s.session_absolute_hours),
            ip=client_ip(request),
            user_agent=(request.headers.get("user-agent") or "")[:500],
        )
    )
    max_age = s.session_absolute_hours * 3600
    response.set_cookie(SESSION_COOKIE, token, max_age=max_age, httponly=True, secure=s.cookie_secure, samesite="lax", path="/")
    response.set_cookie(CSRF_COOKIE, csrf_for(token), max_age=max_age, httponly=False, secure=s.cookie_secure, samesite="lax", path="/")


def clear_session_cookies(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")


async def revoke_user_sessions(db: AsyncSession, user_id: int, except_token_hash: str | None = None) -> None:
    stmt = update(Session).where(Session.user_id == user_id, Session.revoked_at.is_(None))
    if except_token_hash:
        stmt = stmt.where(Session.token_hash != except_token_hash)
    await db.execute(stmt.values(revoked_at=datetime.now(UTC)))


class Principal:
    def __init__(self, user: User, session: Session):
        self.user = user
        self.session = session


async def principal_from_token(db: AsyncSession, token: str | None) -> "Principal | None":
    """Validate a session token (revocation, absolute + idle expiry, active user). Shared by HTTP and WebSocket auth."""
    if not token:
        return None
    row = (
        await db.execute(
            select(Session, User).join(User, User.id == Session.user_id).where(Session.token_hash == _hash_token(token))
        )
    ).first()
    if row is None:
        return None
    sess, user = row
    now = datetime.now(UTC)
    idle_limit = timedelta(minutes=get_settings().session_idle_minutes)
    if sess.revoked_at or sess.expires_at <= now or sess.last_seen_at + idle_limit <= now or not user.is_active:
        return None
    if now - sess.last_seen_at > timedelta(seconds=60):
        sess.last_seen_at = now
        await db.commit()
    return Principal(user, sess)


async def current_principal(request: Request, db: Annotated[AsyncSession, Depends(get_db)]) -> Principal:
    token = request.cookies.get(SESSION_COOKIE)
    p = await principal_from_token(db, token)
    if p is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    # Every authenticated state-changing request must carry the session-bound CSRF token.
    if request.method not in SAFE_METHODS:
        sent = request.headers.get(CSRF_HEADER, "")
        if not hmac.compare_digest(sent, csrf_for(token)):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "CSRF token missing or invalid")
    return p


CurrentPrincipal = Annotated[Principal, Depends(current_principal)]


def require_role(*roles: Role):
    async def dep(p: CurrentPrincipal) -> Principal:
        if p.user.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient role")
        return p

    return Depends(dep)
