import logging
import re
import uuid
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app import observability

from app.api import alerts, auth, bugs, dashboard, github, integrations, runs, users, ws
from app.auth import SAFE_METHODS
from app.config import get_settings

settings = get_settings()
if len(settings.secret_key) < 32:
    raise RuntimeError("SECRET_KEY must be set to at least 32 characters (openssl rand -hex 32)")
observability.setup("api")
log = logging.getLogger("debugops.api")
_RID_OK = re.compile(r"^[A-Za-z0-9._-]{8,64}$")

app = FastAPI(
    title="Realify DebugOps API",
    version="0.1.0",
    docs_url="/api/docs" if settings.api_docs_enabled else None,
    openapi_url="/api/openapi.json" if settings.api_docs_enabled else None,
    redoc_url=None,
)


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception):
    """Unexpected server errors: logged with the request id, surfaced on the dashboard, generic answer to the client."""
    rid = getattr(request.state, "request_id", None) or observability.request_id.get()
    observability.request_id.set(rid)
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    try:
        from app.db import SessionLocal
        from app.events import log_event
        async with SessionLocal() as db:
            log_event(db, "api", f"500 on {request.method} {request.url.path}: {type(exc).__name__} (request {rid})",
                      level="error", type="error", request_id=rid)
            await db.commit()
    except Exception:
        log.exception("could not record error event")
    return JSONResponse({"detail": "Internal server error", "request_id": rid}, status_code=500,
                        headers={"X-Request-ID": rid or ""})


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    incoming = request.headers.get("x-request-id", "")
    rid = incoming if _RID_OK.match(incoming) else uuid.uuid4().hex
    observability.request_id.set(rid)  # for log lines in this request's context
    request.state.request_id = rid  # shared scope: visible to the 500 handler, which runs in an outer context
    response = await call_next(request)
    response.headers["X-Request-ID"] = rid
    return response


@app.middleware("http")
async def origin_check_and_headers(request: Request, call_next):
    # Reject cross-site state-changing requests (defense in depth on top of SameSite + CSRF token).
    origin = request.headers.get("origin")
    if request.method not in SAFE_METHODS and origin:
        host = request.headers.get("host", "")
        if urlsplit(origin).netloc != host and origin not in settings.allowed_origins:
            return JSONResponse({"detail": "Cross-origin request rejected"}, status_code=403)
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("Cache-Control", "no-store")
    return response


api = "/api/v1"
app.include_router(auth.router, prefix=api)
app.include_router(users.router, prefix=api)
app.include_router(dashboard.router, prefix=api)
app.include_router(github.router, prefix=api)
app.include_router(bugs.router, prefix=api)
app.include_router(runs.router, prefix=api)
app.include_router(integrations.router, prefix=api)
app.include_router(alerts.router, prefix=api)
app.include_router(dashboard.health, prefix="/api")
app.include_router(ws.router)
