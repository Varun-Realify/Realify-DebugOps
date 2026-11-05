from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth
from app.audit import audit
from app.db import get_db
from app.models import AuditLog, Role, User
from app.schemas import AuditOut, UserCreate, UserOut, UserUpdate

router = APIRouter(tags=["users"])
Db = Annotated[AsyncSession, Depends(get_db)]
Admin = Annotated[auth.Principal, auth.require_role(Role.admin)]


async def _active_admin_count(db: AsyncSession) -> int:
    return await db.scalar(select(func.count()).where(User.role == Role.admin, User.is_active.is_(True)))


@router.get("/users", response_model=list[UserOut])
async def list_users(_: Admin, db: Db):
    return (await db.execute(select(User).order_by(User.id))).scalars().all()


@router.post("/users", response_model=UserOut, status_code=201)
async def create_user(body: UserCreate, p: Admin, request: Request, db: Db):
    email = body.email.lower()
    if await db.scalar(select(User.id).where(User.email == email)):
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")
    auth.validate_password_strength(body.password)
    user = User(email=email, name=body.name, role=body.role, password_hash=auth.hash_password(body.password))
    db.add(user)
    await db.flush()
    audit(db, "user.created", actor_user_id=p.user.id, target_type="user", target_id=user.id,
          ip=auth.client_ip(request), email=email, role=body.role.value)
    await db.commit()
    return user


@router.patch("/users/{user_id}", response_model=UserOut)
async def update_user(user_id: int, body: UserUpdate, p: Admin, request: Request, db: Db):
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")

    losing_admin = user.role == Role.admin and user.is_active and (
        (body.role is not None and body.role != Role.admin) or body.is_active is False
    )
    if losing_admin and await _active_admin_count(db) <= 1:
        raise HTTPException(status.HTTP_409_CONFLICT, "Cannot remove the last active admin")

    changes = body.model_dump(exclude_unset=True, exclude={"password"})
    for k, v in changes.items():
        setattr(user, k, v)
    revoke = body.is_active is False or body.role is not None
    if body.password is not None:
        auth.validate_password_strength(body.password)
        user.password_hash = auth.hash_password(body.password)
        user.failed_logins, user.locked_until = 0, None
        changes["password"] = "reset"
        revoke = True
    if revoke:  # role, status or password change: force re-login
        await auth.revoke_user_sessions(db, user.id)
    audit(db, "user.updated", actor_user_id=p.user.id, target_type="user", target_id=user.id,
          ip=auth.client_ip(request), changes={k: getattr(v, "value", v) for k, v in changes.items()})
    await db.commit()
    await db.refresh(user)
    return user


@router.delete("/users/{user_id}", status_code=204)
async def deactivate_user(user_id: int, p: Admin, request: Request, db: Db):
    """Soft delete: deactivates and revokes sessions; audit history keeps the user reference."""
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    if user.role == Role.admin and user.is_active and await _active_admin_count(db) <= 1:
        raise HTTPException(status.HTTP_409_CONFLICT, "Cannot remove the last active admin")
    user.is_active = False
    await auth.revoke_user_sessions(db, user.id)
    audit(db, "user.deactivated", actor_user_id=p.user.id, target_type="user", target_id=user.id, ip=auth.client_ip(request))
    await db.commit()
    return Response(status_code=204)


@router.get("/audit-logs", response_model=list[AuditOut])
async def audit_logs(_: Admin, db: Db, limit: int = 100, before_id: int | None = None):
    q = select(AuditLog).order_by(AuditLog.id.desc()).limit(min(max(limit, 1), 500))
    if before_id:
        q = q.where(AuditLog.id < before_id)
    return (await db.execute(q)).scalars().all()
