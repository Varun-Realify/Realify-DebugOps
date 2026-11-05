from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ActivityEvent


def log_event(db: AsyncSession, source: str, message: str, *, level: str = "info", type: str = "action",
              run_id: int | None = None, **data) -> None:
    """Add a live-activity event to the current transaction. Caller commits."""
    db.add(ActivityEvent(source=source, message=message[:5000], level=level, type=type, run_id=run_id, data=data))
