"""Client for AWS DevOps Agent's official remote MCP server.

Uses the official MCP Python SDK (Streamable HTTP transport). Tool names and response shapes follow AWS's official
plugin (github.com/aws/agent-toolkit-for-aws, plugins/aws-agents-for-devsecops):
  investigate(title) -> {status, taskId, executionId, ...}
  get_task(task_id) -> {task: {taskId, status: CREATED|IN_PROGRESS|COMPLETED|FAILED, ...}}
  list_journal_records(execution_id, order, next_token?, limit?) -> {records: [...], next_token?}
  list_recommendations(task_id) -> {recommendations: [...]};  get_recommendation(recommendation_id)

The bearer token is scoped to one AgentSpace and lives only in the worker and the llm-gateway, never in a sandbox.
Investigation output is untrusted text: it is shown and passed on as data, never executed.
"""

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from app.config import Settings

log = logging.getLogger("debugops.devops_agent")

# The tools DebugOps' workflow depends on; the connectivity check fails if any is missing.
REQUIRED_TOOLS = {"investigate", "get_task", "list_journal_records", "list_recommendations"}
# Read-only tools Claude Code may call (through the gateway proxy) to consult an investigation.
READ_ONLY_TOOLS = {"get_task", "list_tasks", "list_journal_records", "list_executions", "list_recommendations",
                   "get_recommendation"}


class DevOpsAgentError(Exception):
    pass


class DevOpsAgentNotConfigured(DevOpsAgentError):
    pass


class DevOpsAgentAuthError(DevOpsAgentError):
    pass


class DevOpsAgentUnavailable(DevOpsAgentError):
    pass


class DevOpsAgentThrottled(DevOpsAgentError):
    pass


class DevOpsAgentToolError(DevOpsAgentError):
    pass


def _leaf(exc: BaseException) -> BaseException:
    """The SDK runs on anyio task groups: unwrap exception groups to the underlying error."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


def _for_status(code: int) -> DevOpsAgentError | None:
    if code == 401:
        return DevOpsAgentAuthError("token invalid or expired (HTTP 401): create a new one in the DevOps Agent Operator web app")
    if code == 403:
        return DevOpsAgentAuthError("token lacks scope (HTTP 403): it needs the read and operate scopes")
    if code == 429:
        return DevOpsAgentThrottled("throttled by AWS DevOps Agent (HTTP 429)")
    if code >= 500:
        return DevOpsAgentUnavailable(f"AWS DevOps Agent returned HTTP {code}")
    return None


def classify(exc: BaseException, http_errors: list[int] = ()) -> DevOpsAgentError:
    e = _leaf(exc)
    if isinstance(e, DevOpsAgentError):
        return e
    # The MCP SDK reports HTTP failures as a generic MCPError without the status: use what the HTTP hook saw.
    for code in reversed(list(http_errors)):
        if (mapped := _for_status(code)) is not None:
            return mapped
    if isinstance(e, httpx2.HTTPStatusError):
        code = e.response.status_code
        if code == 401:
            return DevOpsAgentAuthError("token invalid or expired (HTTP 401): create a new one in the DevOps Agent Operator web app")
        if code == 403:
            return DevOpsAgentAuthError("token lacks scope (HTTP 403): it needs the read and operate scopes")
        if code == 429:
            return DevOpsAgentThrottled("throttled by AWS DevOps Agent (HTTP 429)")
        return DevOpsAgentUnavailable(f"AWS DevOps Agent returned HTTP {code}")
    if isinstance(e, (httpx2.TimeoutException, TimeoutError)):
        return DevOpsAgentUnavailable("timed out talking to AWS DevOps Agent")
    if isinstance(e, (httpx2.TransportError, OSError)):
        return DevOpsAgentUnavailable(f"cannot reach AWS DevOps Agent ({type(e).__name__})")
    return DevOpsAgentError(f"{type(e).__name__}: {str(e)[:300]}")


def _field(obj: Any, *names: str) -> Any:
    """MCP SDK 2.x uses snake_case (is_error, structured_content); 1.x used camelCase. Accept both."""
    for n in names:
        if hasattr(obj, n):
            return getattr(obj, n)
    return None


def _payload(result: Any) -> dict:
    """Tool result -> dict: structured content if present, else JSON (or text) from the text content."""
    text = "".join(getattr(c, "text", "") for c in (getattr(result, "content", None) or []))
    if _field(result, "is_error", "isError"):
        if "Throttling" in text:
            raise DevOpsAgentThrottled(text[:300])
        raise DevOpsAgentToolError(text[:1000] or "tool returned an error")
    structured = _field(result, "structured_content", "structuredContent")
    if isinstance(structured, dict) and structured:
        # Tools whose return value is not an object are wrapped as {"result": ...} by the SDK.
        return structured
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {"result": data}
    except ValueError:
        return {"text": text}


class DevOpsAgentClient:
    def __init__(self, settings: Settings, transport: httpx2.AsyncBaseTransport | None = None, sleep=asyncio.sleep):
        self.s = settings
        self._transport = transport
        self._sleep = sleep  # injectable for tests

    @asynccontextmanager
    async def session(self):
        if not self.s.devops_agent_configured:
            raise DevOpsAgentNotConfigured("AWS DevOps Agent is not configured (DEVOPS_AGENT_TOKEN)")
        http_errors: list[int] = []

        async def on_response(response):
            if response.status_code >= 400:
                http_errors.append(response.status_code)

        http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {self.s.devops_agent_token}"},
                                  timeout=httpx2.Timeout(self.s.devops_agent_timeout_seconds, connect=15),
                                  transport=self._transport, event_hooks={"response": [on_response]})
        try:
            async with http, streamable_http_client(self.s.devops_agent_endpoint, http_client=http) as (read, write):
                async with ClientSession(read, write, read_timeout_seconds=self.s.devops_agent_timeout_seconds) as session:
                    await session.initialize()
                    yield session
        except BaseException as e:  # noqa: BLE001 — normalise SDK/transport errors (incl. exception groups)
            if isinstance(e, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            raise classify(e, http_errors) from e

    async def list_tools(self) -> list[str]:
        async with self.session() as s:
            res = await s.list_tools()
            return sorted(t.name for t in res.tools)

    async def call(self, tool: str, arguments: dict | None = None, *, retries: int = 3) -> dict:
        """Call one tool; back off on throttling (60s, 90s, 120s per AWS guidance)."""
        for attempt in range(retries + 1):
            try:
                async with self.session() as s:
                    return _payload(await s.call_tool(tool, arguments or {},
                                                      read_timeout_seconds=self.s.devops_agent_timeout_seconds))
            except DevOpsAgentThrottled:
                if attempt == retries:
                    raise
                await self._sleep(self.backoff(attempt))
        raise AssertionError("unreachable")

    @staticmethod
    def backoff(attempt: int) -> float:
        return 60 + 30 * attempt


async def check_connectivity(client: DevOpsAgentClient) -> dict:
    """Live check: MCP initialize + tools/list against the real endpoint. Never includes the token."""
    result = {"ok": False, "endpoint": client.s.devops_agent_endpoint, "auth": client.s.devops_agent_auth,
              "checked_at": datetime.now(UTC).isoformat(), "tools": [], "missing_tools": [], "error": None}
    try:
        tools = await client.list_tools()
    except DevOpsAgentError as e:
        result["error"] = str(e)
        return result
    result["tools"] = tools
    result["missing_tools"] = sorted(REQUIRED_TOOLS - set(tools))
    result["ok"] = not result["missing_tools"]
    if result["missing_tools"]:
        result["error"] = (f"required tools missing: {', '.join(result['missing_tools'])}"
                           + (" (AWS lists investigate only for tokens with the operate scope)" if "investigate" in result["missing_tools"] else ""))
    return result
