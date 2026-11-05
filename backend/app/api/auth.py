from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth
from app.audit import audit
from app.config import get_settings
from app.db import get_db
from app.models import Session, User
from app.schemas import LoginIn, PasswordChange, UserOut

router = APIRouter(prefix="/auth", tags=["auth"])
Db = Annotated[AsyncSession, Depends(get_db)]

INVALID = "Invalid email or password"

# ponytail: in-process sliding window per IP. Fine for one api replica; use Redis or the reverse proxy if scaled out.
_attempts: dict[str, list[float]] = {}


def _throttle(ip: str | None) -> None:
    import time
    now, key = time.monotonic(), ip or "unknown"
    window = [t for t in _attempts.get(key, []) if now - t < 60]
    if len(window) >= get_settings().login_rate_per_minute:
        _attempts[key] = window
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many login attempts; try again in a minute")
    window.append(now)
    _attempts[key] = window
    if len(_attempts) > 10_000:  # bound memory under a spray from many IPs
        _attempts.clear()


@router.post("/login", response_model=UserOut)
async def login(body: LoginIn, request: Request, response: Response, db: Db):
    s = get_settings()
    now = datetime.now(UTC)
    ip = auth.client_ip(request)
    _throttle(ip)
    email = body.email.lower()
    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()

    ok = auth.verify_password(user.password_hash if user else None, body.password)
    locked = bool(user and user.locked_until and user.locked_until > now)

    if not user or not ok or locked or not user.is_active:
        if user and not locked:
            if not ok:
                user.failed_logins += 1
                if user.failed_logins >= s.login_max_failures:
                    user.locked_until = now + timedelta(minutes=s.login_lockout_minutes)
                    user.failed_logins = 0
                    audit(db, "auth.lockout", actor_user_id=user.id, target_type="user", target_id=user.id, ip=ip)
        audit(db, "auth.login_failed", actor_user_id=user.id if user else None, ip=ip, email=email,
              reason="locked" if locked else "inactive" if (user and ok) else "bad_credentials")
        await db.commit()
        # Same response for every failure mode: no account enumeration.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, INVALID)

    # Rotate: revoke any session this browser already carried.
    old = request.cookies.get(auth.SESSION_COOKIE)
    if old:
        prev = (await db.execute(select(Session).where(Session.token_hash == auth._hash_token(old)))).scalar_one_or_none()
        if prev and prev.revoked_at is None:
            prev.revoked_at = now

    user.failed_logins = 0
    user.locked_until = None
    user.last_login_at = now
    await auth.create_session(db, user, request, response)
    audit(db, "auth.login", actor_user_id=user.id, ip=ip)
    await db.commit()
    return user


@router.post("/logout", status_code=204)
async def logout(p: auth.CurrentPrincipal, request: Request, response: Response, db: Db):
    sess = await db.get(Session, p.session.id)
    sess.revoked_at = datetime.now(UTC)
    audit(db, "auth.logout", actor_user_id=p.user.id, ip=auth.client_ip(request))
    await db.commit()
    auth.clear_session_cookies(response)
    response.status_code = 204
    return response


@router.get("/me", response_model=UserOut)
async def me(p: auth.CurrentPrincipal):
    return p.user


@router.post("/change-password", status_code=204)
async def change_password(body: PasswordChange, p: auth.CurrentPrincipal, request: Request, db: Db):
    user = await db.get(User, p.user.id)
    if not auth.verify_password(user.password_hash, body.current_password):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Current password is incorrect")
    auth.validate_password_strength(body.new_password)
    user.password_hash = auth.hash_password(body.new_password)
    await auth.revoke_user_sessions(db, user.id, except_token_hash=p.session.token_hash)
    audit(db, "auth.password_changed", actor_user_id=user.id, target_type="user", target_id=user.id, ip=auth.client_ip(request))
    await db.commit()
    return Response(status_code=204)
