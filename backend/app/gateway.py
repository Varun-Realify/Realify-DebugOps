"""LLM gateway: the only path from a sandbox to the Claude API.

Sandboxes authenticate with a per-run token (minted by the worker, stored hashed, revoked when the run's
sandbox is destroyed). The gateway swaps it for the real Anthropic key, enforces an endpoint allowlist, a
model allowlist and per-run request/token budgets, and records usage. Run: uvicorn app.gateway:app --port 8090
"""

import hashlib
import json
import logging
from datetime import UTC, datetime

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy import select, update

from app.config import get_settings
from app.db import SessionLocal
from app.models import GatewayToken

from app import observability

observability.setup("llm-gateway")
log = logging.getLogger("debugops.gateway")
s = get_settings()
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
ALLOWED_PATHS = {"/v1/messages", "/v1/messages/count_tokens"}
FORWARD_HEADERS = ("anthropic-version", "anthropic-beta", "content-type", "accept")
_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(base_url=s.anthropic_base_url.rstrip("/"), timeout=httpx.Timeout(900.0, connect=15.0))
    return _client


def _err(status: int, message: str) -> JSONResponse:
    # Anthropic-shaped error so Claude Code reports it clearly.
    return JSONResponse({"type": "error", "error": {"type": "permission_error" if status in (401, 403) else "invalid_request_error",
                                                    "message": f"DebugOps gateway: {message}"}}, status_code=status)


async def _authorize(raw: str | None) -> GatewayToken | JSONResponse:
    if not raw:
        return _err(401, "missing credentials")
    async with SessionLocal() as db:
        tok = (await db.execute(select(GatewayToken).where(GatewayToken.token_hash == hashlib.sha256(raw.encode()).hexdigest()))).scalar_one_or_none()
        if tok is None or tok.revoked_at is not None or tok.expires_at <= datetime.now(UTC):
            return _err(401, "invalid, expired or revoked run token")
        if tok.request_count >= s.run_max_llm_requests:
            return _err(429, f"run request budget exhausted ({s.run_max_llm_requests})")
        if tok.tokens_in + tok.tokens_out >= s.run_max_llm_tokens:
            return _err(429, f"run token budget exhausted ({s.run_max_llm_tokens:,})")
        # Atomic increment: concurrent requests cannot overshoot the request budget by more than in-flight count.
        await db.execute(update(GatewayToken).where(GatewayToken.id == tok.id).values(request_count=GatewayToken.request_count + 1))
        await db.commit()
        return tok


async def _record(token_id: int, tin: int, tout: int) -> None:
    if not (tin or tout):
        return
    async with SessionLocal() as db:
        await db.execute(update(GatewayToken).where(GatewayToken.id == token_id)
                         .values(tokens_in=GatewayToken.tokens_in + tin, tokens_out=GatewayToken.tokens_out + tout))
        await db.commit()


def _usage_in(u: dict) -> int:
    return int(u.get("input_tokens") or 0) + int(u.get("cache_read_input_tokens") or 0) + int(u.get("cache_creation_input_tokens") or 0)


@app.get("/health")
async def health():
    return {"status": "ok", "configured": bool(s.anthropic_api_key)}


@app.post("/v1/{path:path}")
async def proxy(path: str, request: Request):
    full = "/v1/" + path
    if full not in ALLOWED_PATHS:
        return _err(404, f"endpoint {full} is not allowed")
    if not s.anthropic_api_key:
        return _err(503, "ANTHROPIC_API_KEY is not configured")
    raw = request.headers.get("x-api-key") or request.headers.get("authorization", "").removeprefix("Bearer ").strip() or None
    tok = await _authorize(raw)
    if isinstance(tok, JSONResponse):
        return tok
    body = await request.body()
    if len(body) > 30_000_000:
        return _err(413, "request too large")
    try:
        payload = json.loads(body)
    except ValueError:
        return _err(400, "body is not JSON")
    if payload.get("model") not in s.llm_gateway_allowed_models:
        return _err(400, f"model {payload.get('model')!r} is not allowed")

    headers = {h: request.headers[h] for h in FORWARD_HEADERS if h in request.headers}
    headers["x-api-key"] = s.anthropic_api_key
    upstream = client().build_request("POST", full, params=dict(request.query_params), content=body, headers=headers)
    try:
        resp = await client().send(upstream, stream=True)
    except httpx.HTTPError as e:
        return _err(502, f"upstream unreachable: {type(e).__name__}")

    if "text/event-stream" not in resp.headers.get("content-type", ""):
        data = await resp.aread()
        await resp.aclose()
        try:
            usage = json.loads(data).get("usage") or {}
            await _record(tok.id, _usage_in(usage), int(usage.get("output_tokens") or 0))
        except (ValueError, AttributeError):
            pass
        return Response(data, status_code=resp.status_code, media_type=resp.headers.get("content-type"))

    async def stream():
        tin = tout = 0
        buf = b""
        try:
            async for chunk in resp.aiter_bytes():  # decoded: we do not forward content-encoding
                yield chunk
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.startswith(b"data:"):
                        continue
                    try:
                        ev = json.loads(line[5:])
                    except ValueError:
                        continue
                    if ev.get("type") == "message_start":
                        tin = _usage_in((ev.get("message") or {}).get("usage") or {})
                    elif ev.get("type") == "message_delta":
                        tout = int((ev.get("usage") or {}).get("output_tokens") or tout)
        finally:
            await resp.aclose()
            await _record(tok.id, tin, tout)

    return StreamingResponse(stream(), status_code=resp.status_code, media_type="text/event-stream",
                             headers={"cache-control": "no-cache"})


# ---------------- read-only MCP proxy to AWS DevOps Agent (for Claude Code inside sandboxes) ----------------

MCP_METHODS = {"initialize", "notifications/initialized", "notifications/cancelled", "ping", "tools/list", "tools/call"}
MCP_FORWARD_HEADERS = ("content-type", "accept", "mcp-session-id", "mcp-protocol-version", "last-event-id")


def _rpc_error(msg_id, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"DebugOps gateway: {message}"}}


def _filter_tools(obj):
    """Hide every tool except the read-only ones from tools/list results."""
    from app.integrations.devops_agent import READ_ONLY_TOOLS
    if isinstance(obj, dict) and isinstance((obj.get("result") or {}).get("tools"), list):
        obj["result"]["tools"] = [t for t in obj["result"]["tools"] if t.get("name") in READ_ONLY_TOOLS]
    return obj


@app.post("/mcp/aws-devops-agent")
async def devops_agent_mcp(request: Request):
    """Claude Code -> (per-run token) -> here -> (real DevOps Agent token) -> AWS. Only read-only tools are allowed:
    Claude Code can consult an investigation DebugOps started, but cannot start investigations, run mitigations or
    touch admin tools (access tokens, agent spaces)."""
    from app.integrations.devops_agent import READ_ONLY_TOOLS
    if not s.devops_agent_configured:
        return _err(503, "AWS DevOps Agent is not configured")
    raw = request.headers.get("authorization", "").removeprefix("Bearer ").strip() or None
    tok = await _authorize(raw)
    if isinstance(tok, JSONResponse):
        return tok
    body = await request.body()
    if len(body) > 1_000_000:
        return _err(413, "request too large")
    try:
        payload = json.loads(body)
    except ValueError:
        return _err(400, "body is not JSON")
    messages = payload if isinstance(payload, list) else [payload]
    for m in messages:
        method = m.get("method") if isinstance(m, dict) else None
        if method is None:  # a response from the client (e.g. to a server request): not used here
            return JSONResponse(_rpc_error(None, "unexpected message"), status_code=400)
        if method not in MCP_METHODS:
            return JSONResponse(_rpc_error(m.get("id"), f"method {method!r} not allowed"))
        if method == "tools/call" and (m.get("params") or {}).get("name") not in READ_ONLY_TOOLS:
            log.warning("blocked DevOps Agent tool %r for run token %s", (m.get("params") or {}).get("name"), tok.id)
            return JSONResponse(_rpc_error(m.get("id"), f"tool {(m.get('params') or {}).get('name')!r} is not allowed (read-only access)"))

    headers = {h: request.headers[h] for h in MCP_FORWARD_HEADERS if h in request.headers}
    headers["authorization"] = f"Bearer {s.devops_agent_token}"
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(s.devops_agent_timeout_seconds + 30, connect=15)) as c:
            resp = await c.post(s.devops_agent_endpoint, content=body, headers=headers)
    except httpx.HTTPError as e:
        return _err(502, f"AWS DevOps Agent unreachable: {type(e).__name__}")
    out_headers = {k: v for k, v in resp.headers.items() if k.lower() == "mcp-session-id"}
    ctype = resp.headers.get("content-type", "")
    if resp.status_code >= 400:
        return _err(502 if resp.status_code >= 500 else resp.status_code, f"upstream returned HTTP {resp.status_code}")
    if "text/event-stream" in ctype:  # SSE: rewrite each data line (tools/list filtering)
        lines = []
        for line in resp.text.split("\n"):
            if line.startswith("data:"):
                try:
                    line = "data: " + json.dumps(_filter_tools(json.loads(line[5:])))
                except ValueError:
                    pass
            lines.append(line)
        return Response("\n".join(lines), status_code=resp.status_code, media_type="text/event-stream", headers=out_headers)
    if "application/json" in ctype and resp.content:
        data = resp.json()
        data = [_filter_tools(d) for d in data] if isinstance(data, list) else _filter_tools(data)
        return JSONResponse(data, status_code=resp.status_code, headers=out_headers)
    return Response(resp.content, status_code=resp.status_code, media_type=ctype or None, headers=out_headers)


@app.delete("/mcp/aws-devops-agent")
async def devops_agent_mcp_close(request: Request):
    raw = request.headers.get("authorization", "").removeprefix("Bearer ").strip() or None
    tok = await _authorize(raw)
    if isinstance(tok, JSONResponse):
        return tok
    sid = request.headers.get("mcp-session-id")
    if sid and s.devops_agent_configured:
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                await c.delete(s.devops_agent_endpoint, headers={"authorization": f"Bearer {s.devops_agent_token}", "mcp-session-id": sid})
        except httpx.HTTPError:
            pass
    return Response(status_code=204)


@app.api_route("/{path:path}", methods=["GET", "PUT", "PATCH", "DELETE"])
async def deny(path: str):
    raise HTTPException(404, "not found")  # includes GET /mcp/... (no server-initiated streams through the proxy)
