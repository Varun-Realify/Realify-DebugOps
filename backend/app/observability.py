"""Centralized logging and error monitoring.

- Every service logs JSON lines to stdout (LOG_FORMAT=json, default) so any collector (Docker logging driver,
  Loki/Promtail, Fluent Bit, CloudWatch, Google Cloud Logging) can centralize them. Each line carries `service`
  and, for API requests, `request_id`.
- Optional error tracking: set SENTRY_DSN to send unhandled exceptions to Sentry (or a Sentry-compatible
  service such as GlitchTip). No request bodies or cookies are sent.
"""

import contextvars
import json
import logging
import sys
from datetime import UTC, datetime

from app.config import get_settings

request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str):
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        entry = {"ts": datetime.fromtimestamp(record.created, UTC).isoformat(), "level": record.levelname.lower(),
                 "service": self.service, "logger": record.name, "msg": record.getMessage()}
        rid = request_id.get()
        if rid:
            entry["request_id"] = rid
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)[-8000:]
        return json.dumps(entry, default=str)


def setup(service: str) -> None:
    s = get_settings()
    handler = logging.StreamHandler(sys.stdout)
    if s.log_format == "json":
        handler.setFormatter(JsonFormatter(service))
    else:
        handler.setFormatter(logging.Formatter(f"%(asctime)s %(levelname)s {service} %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(s.log_level)
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if s.sentry_dsn:
        import sentry_sdk
        sentry_sdk.init(dsn=s.sentry_dsn, environment=s.environment_name, server_name=service,
                        send_default_pii=False, max_request_body_size="never", traces_sample_rate=0.0)
        sentry_sdk.set_tag("service", service)
