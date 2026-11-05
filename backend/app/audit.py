from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog


def audit(
    db: AsyncSession,
    action: str,
    *,
    actor_user_id: int | None = None,
    actor_type: str = "user",
    target_type: str | None = None,
    target_id: str | int | None = None,
    ip: str | None = None,
    **data,
) -> None:
    """Add an audit row to the current transaction. Caller commits."""
    db.add(
        AuditLog(
            action=action,
            actor_user_id=actor_user_id,
            actor_type=actor_type,
            target_type=target_type,
            target_id=None if target_id is None else str(target_id),
            ip=ip,
            data=data,
        )
    )
