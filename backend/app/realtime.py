"""Live updates: PostgreSQL LISTEN/NOTIFY -> in-process fan-out -> WebSocket clients.

Database triggers (migration 0005) NOTIFY on new activity events and on run/scan status changes, so every
service that writes to the database publishes for free. One LISTEN connection per api process.
"""

import asyncio
import json
import logging

import asyncpg

from app.config import get_settings

log = logging.getLogger("debugops.realtime")
CHANNEL = "debugops_events"
QUEUE_SIZE = 1000


class Broker:
    def __init__(self):
        self.subscribers: set[asyncio.Queue] = set()
        self._task: asyncio.Task | None = None

    def subscribe(self) -> asyncio.Queue:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._listen())
        q: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_SIZE)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)

    def publish(self, payload: dict) -> None:
        for q in list(self.subscribers):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:  # slow client: drop it; it reconnects and replays from its last event id
                q.put_nowait({"kind": "overflow"}) if q.empty() else None
                self.subscribers.discard(q)

    async def _listen(self) -> None:
        dsn = get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://")
        while self.subscribers or self._task is asyncio.current_task():
            try:
                conn = await asyncpg.connect(dsn)
                try:
                    await conn.add_listener(CHANNEL, lambda *args: self._on_notify(args[-1]))
                    while True:
                        await asyncio.sleep(30)
                        await conn.execute("SELECT 1")  # keepalive; raises if the connection died
                finally:
                    await conn.close()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("LISTEN connection lost; reconnecting in 3s")
                await asyncio.sleep(3)

    def _on_notify(self, raw: str) -> None:
        try:
            self.publish(json.loads(raw))
        except ValueError:
            log.warning("bad NOTIFY payload")


broker = Broker()
