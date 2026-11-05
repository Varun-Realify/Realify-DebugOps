import asyncio
from urllib.parse import urlsplit

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import select

from app.auth import SESSION_COOKIE, principal_from_token
from app.config import get_settings
from app.db import SessionLocal
from app.models import ActivityEvent
from app.realtime import broker

router = APIRouter()
REAUTH_SECONDS = 60
BACKLOG_LIMIT = 500


def _event_dict(e: ActivityEvent) -> dict:
    return {"kind": "event", "id": e.id, "run_id": e.run_id, "level": e.level, "type": e.type, "source": e.source,
            "message": e.message[:1500], "ts": e.ts.isoformat(), "scan_id": (e.data or {}).get("scan_id")}


@router.websocket("/api/v1/ws")
async def events_ws(ws: WebSocket, after: int = 0, run_id: int | None = None):
    """Live stream of activity events and run/scan status changes.

    Auth: the session cookie (same rules as HTTP). Origin must match the host (CSWSH protection).
    `after`: replay events with id > after first, so reconnecting clients miss nothing. `run_id`: filter to one run.
    """
    origin = ws.headers.get("origin")
    if origin and urlsplit(origin).netloc != ws.headers.get("host", "") and origin not in get_settings().allowed_origins:
        await ws.close(code=1008)
        return
    token = ws.cookies.get(SESSION_COOKIE)
    async with SessionLocal() as db:
        if await principal_from_token(db, token) is None:
            await ws.close(code=1008)
            return
    await ws.accept()
    q = broker.subscribe()
    try:
        async with SessionLocal() as db:
            stmt = select(ActivityEvent).where(ActivityEvent.id > after).order_by(ActivityEvent.id.desc()).limit(BACKLOG_LIMIT)
            if run_id:
                stmt = stmt.where(ActivityEvent.run_id == run_id)
            for e in reversed((await db.execute(stmt)).scalars().all()):
                await ws.send_json(_event_dict(e))
        await ws.send_json({"kind": "ready"})
        loop = asyncio.get_running_loop()
        next_auth = loop.time() + REAUTH_SECONDS
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=15)
            except TimeoutError:
                msg = {"kind": "ping"}
            if loop.time() >= next_auth:  # revoked / expired sessions are cut off within a minute
                async with SessionLocal() as db:
                    if await principal_from_token(db, token) is None:
                        await ws.close(code=1008)
                        return
                next_auth = loop.time() + REAUTH_SECONDS
            if msg.get("kind") == "overflow":
                await ws.close(code=1013)  # client reconnects with ?after=<last id>
                return
            if run_id and msg.get("kind") == "event" and msg.get("run_id") != run_id:
                continue
            if run_id and msg.get("kind") == "run" and msg.get("id") != run_id:
                continue
            await ws.send_json(msg)
    except WebSocketDisconnect:
        pass
    finally:
        broker.unsubscribe(q)
