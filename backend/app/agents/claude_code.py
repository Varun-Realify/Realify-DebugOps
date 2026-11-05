"""Run Claude Code headless inside a sandbox for agents that must read or edit the repository.

Claude Code talks to the DebugOps LLM gateway (ANTHROPIC_BASE_URL) with a short-lived per-run token
(ANTHROPIC_API_KEY). The real Anthropic key never enters the sandbox.
"""

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Protocol, TypeVar
from urllib.parse import urlsplit

from pydantic import BaseModel, ValidationError

from app.agents.llm import AgentError
from app.config import Settings
from app.sandbox.runner import Sandbox

T = TypeVar("T", bound=BaseModel)

READ_TOOLS = ["Read", "Grep", "Glob", "LS"]
EDIT_TOOLS = ["Read", "Grep", "Glob", "LS", "Edit", "MultiEdit", "Write"]
NEVER_TOOLS = ["WebFetch", "WebSearch", "Task", "NotebookEdit"]


@dataclass
class CodeAgentResult:
    parsed: BaseModel
    text: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    turns: int


class CodeAgent(Protocol):
    async def run(self, *, agent: str, prompt: str, schema: type[T], tools: list[str], edit: bool,
                  on_event=None) -> CodeAgentResult: ...


MAX_LIVE_EVENTS = 400  # per agent step


def describe(event: dict) -> list[tuple[str, str]]:
    """Turn one Claude Code stream-json event into live-feed items: (type, message)."""
    out: list[tuple[str, str]] = []
    if event.get("type") != "assistant":
        return out
    for block in (event.get("message") or {}).get("content") or []:
        if block.get("type") == "text" and block.get("text", "").strip():
            text = " ".join(block["text"].split())
            if not text.startswith(("{", "```")):
                out.append(("thought", text[:400]))
        elif block.get("type") == "tool_use":
            name, inp = block.get("name", ""), block.get("input") or {}
            path = str(inp.get("file_path") or inp.get("path") or "").removeprefix("/workspace/src/")
            if name == "Read":
                out.append(("file_read", f"Reading {path}" + (f" (from line {inp['offset']})" if inp.get("offset") else "")))
            elif name in ("Edit", "MultiEdit"):
                out.append(("edit", f"Editing {path}"))
            elif name == "Write":
                out.append(("edit", f"Writing {path}"))
            elif name == "Grep":
                out.append(("search", f"Searching for “{str(inp.get('pattern', ''))[:80]}”" + (f" in {path}" if path else "")))
            elif name in ("Glob", "LS"):
                out.append(("search", f"Listing {inp.get('pattern') or path or 'files'}"))
            elif name == "Bash":
                out.append(("command", f"$ {str(inp.get('command', ''))[:300]}"))
            else:
                out.append(("action", f"{name}"))
    return out


def extract_json(text: str) -> dict:
    """The final answer must end with one JSON object; accept a ```json fence or a bare object."""
    fence = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidates = fence[::-1] if fence else []
    if not candidates:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            candidates = [text[start:end + 1]]
    for c in candidates:
        try:
            return json.loads(c)
        except ValueError:
            continue
    raise ValueError("no JSON object found in the agent's final answer")


def _parse_cli_output(output: str) -> dict:
    """`claude -p --output-format stream-json` ends with one `{"type": "result", ...}` line."""
    for line in reversed(output.strip().splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if obj.get("type") == "result":
            return obj
    raise AgentError(f"Claude Code produced no result: {output.strip()[-500:]}")


class ClaudeCodeRunner:
    def __init__(self, sandbox: Sandbox, gateway_token: str, settings: Settings):
        self.sb, self.token, self.s = sandbox, gateway_token, settings
        host = urlsplit(settings.llm_gateway_url).hostname or "llm-gateway"
        self.env = {
            "ANTHROPIC_BASE_URL": settings.llm_gateway_url, "ANTHROPIC_API_KEY": gateway_token,
            "NO_PROXY": f"localhost,127.0.0.1,{host}", "no_proxy": f"localhost,127.0.0.1,{host}",
            "DISABLE_AUTOUPDATER": "1", "DISABLE_TELEMETRY": "1", "DISABLE_ERROR_REPORTING": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "CLAUDE_CONFIG_DIR": "/tmp/.claude",
            # Background/small-model calls must also use a model the gateway allows.
            "ANTHROPIC_SMALL_FAST_MODEL": "claude-haiku-5-5", "ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-haiku-5-5",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-sonnet-5-5", "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus-5-5",
        }

    async def _invoke(self, args: list[str], on_event=None) -> dict:
        """Run the CLI with stream-json output: every tool call / thought is forwarded to on_event live."""
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        sent = 0

        def on_line(line: str) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, line)

        async def pump():
            nonlocal sent
            while True:
                line = await queue.get()
                if line is None:
                    return
                if on_event is None or sent >= MAX_LIVE_EVENTS or not line.startswith("{"):
                    continue
                try:
                    items = describe(json.loads(line))
                except ValueError:
                    continue
                for kind, message in items:
                    sent += 1
                    await on_event(kind, message)

        pump_task = asyncio.create_task(pump())
        try:
            r = await asyncio.to_thread(self.sb.exec_stream, ["claude", *args, "--verbose"], self.s.agent_step_timeout_seconds,
                                        on_line, env=self.env)
        finally:
            loop.call_soon_threadsafe(queue.put_nowait, None)
            await pump_task
        if r.timed_out:
            raise AgentError(f"Claude Code timed out after {self.s.agent_step_timeout_seconds}s")
        res = _parse_cli_output(r.output)
        if res.get("is_error") and res.get("subtype") not in ("error_max_turns",):
            raise AgentError(f"Claude Code error: {str(res.get('result') or res.get('subtype'))[:500]}")
        return res

    async def run(self, *, agent: str, prompt: str, schema: type[T], tools: list[str], edit: bool,
                  on_event=None) -> CodeAgentResult:
        base = ["--output-format", "stream-json", "--model", self.s.agent_model,
                "--allowedTools", ",".join(tools), "--disallowedTools", ",".join(NEVER_TOOLS),
                "--permission-mode", "acceptEdits" if edit else "default"]
        res = await self._invoke(["-p", prompt, "--max-turns", str(self.s.claude_code_max_turns), *base], on_event)
        usage_in = usage_out = 0
        cost, turns = 0.0, 0

        def account(r: dict):
            nonlocal usage_in, usage_out, cost, turns
            u = r.get("usage") or {}
            usage_in += int(u.get("input_tokens", 0)) + int(u.get("cache_read_input_tokens", 0)) + int(u.get("cache_creation_input_tokens", 0))
            usage_out += int(u.get("output_tokens", 0))
            cost += float(r.get("total_cost_usd") or 0)
            turns += int(r.get("num_turns") or 0)

        account(res)
        text = str(res.get("result") or "")
        for attempt in range(2):
            try:
                parsed = schema.model_validate(extract_json(text))
                return CodeAgentResult(parsed, text, usage_in, usage_out, round(cost, 6), turns)
            except (ValueError, ValidationError) as e:
                if attempt == 1 or not res.get("session_id"):
                    raise AgentError(f"{agent}: final answer did not match the required schema: {str(e)[:300]}") from e
                fix = (f"Your final answer was not a valid JSON object for the required schema ({str(e)[:400]}). "
                       f"Reply with ONLY that JSON object, no other text. Schema: {json.dumps(schema.model_json_schema())}")
                res = await self._invoke(["-p", fix, "--resume", res["session_id"], "--max-turns", "2", *base])
                account(res)
                text = str(res.get("result") or "")
        raise AssertionError("unreachable")
