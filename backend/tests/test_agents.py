import hashlib
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from app import gateway
from app.agents.claude_code import ClaudeCodeRunner, extract_json
from app.agents.llm import AgentError, AnthropicLLM
from app.agents.schemas import FixResult, ReviewVerdict
from app.config import get_settings
from app.db import SessionLocal
from app.models import Bug, GatewayToken, Repository, WorkflowRun
from app.sandbox.runner import ExecResult


# ---------------- JSON extraction / Claude Code runner ----------------

def test_extract_json_variants():
    assert extract_json('Done.\n```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('first ```json\n{"a": 1}\n``` then ```json\n{"a": 2}\n```') == {"a": 2}  # last fence wins
    assert extract_json('result: {"a": {"b": 2}} end') == {"a": {"b": 2}}
    with pytest.raises(ValueError):
        extract_json("no json here")


class ScriptedSandbox:
    def __init__(self, outputs):
        self.outputs, self.calls = list(outputs), []

    def exec(self, cmd, timeout, workdir=None, env=None):
        self.calls.append((cmd, env))
        return ExecResult(0, self.outputs.pop(0), False, 10)

    def exec_stream(self, cmd, timeout, on_line, workdir=None, env=None):
        r = self.exec(cmd, timeout, workdir, env)
        for ln in r.output.splitlines():
            on_line(ln)
        return r


TOOL_EVENTS = "\n".join(json.dumps(e) for e in [
    {"type": "system", "subtype": "init"},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "Let me look at the auth module."},
                                                  {"type": "tool_use", "name": "Read", "input": {"file_path": "/workspace/src/app/auth.py"}}]}},
    {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "pytest -q tests/test_auth.py"}}]}},
    {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Edit", "input": {"file_path": "/workspace/src/app/auth.py"}}]}},
])


def cli(result: str, session="s1", **kw) -> str:
    return "some stderr noise\n" + TOOL_EVENTS + "\n" + json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": result,
                                               "session_id": session, "num_turns": 4, "total_cost_usd": 0.12,
                                               "usage": {"input_tokens": 1000, "output_tokens": 200, "cache_read_input_tokens": 500}, **kw})


async def test_claude_code_runner_env_and_retry():
    sb = ScriptedSandbox([cli("I changed things but forgot JSON"),
                          cli('```json\n{"summary": "s", "rationale": "r", "files_changed": ["a.py"]}\n```')])
    r = ClaudeCodeRunner(sb, "dgw_runtoken", get_settings())
    feed = []

    async def on_event(kind, message):
        feed.append((kind, message))
    res = await r.run(agent="fix", prompt="fix it", schema=FixResult, tools=["Read", "Edit"], edit=True, on_event=on_event)
    assert feed[:4] == [("thought", "Let me look at the auth module."), ("file_read", "Reading app/auth.py"),
                        ("command", "$ pytest -q tests/test_auth.py"), ("edit", "Editing app/auth.py")]
    assert res.parsed.files_changed == ["a.py"] and res.tokens_in == 3000 and res.tokens_out == 400 and res.turns == 8
    (cmd1, env1), (cmd2, _) = sb.calls
    assert env1["ANTHROPIC_API_KEY"] == "dgw_runtoken" and env1["ANTHROPIC_BASE_URL"] == get_settings().llm_gateway_url
    assert get_settings().anthropic_api_key not in json.dumps([cmd1, env1])  # real key never enters the sandbox
    assert "llm-gateway" in env1["NO_PROXY"]
    assert "--permission-mode" in cmd1 and cmd1[cmd1.index("--permission-mode") + 1] == "acceptEdits"
    assert "WebFetch" in cmd1[cmd1.index("--disallowedTools") + 1]
    assert "--resume" in cmd2 and cmd2[cmd2.index("--resume") + 1] == "s1"
    assert cmd1[cmd1.index("--output-format") + 1] == "stream-json" and "--verbose" in cmd1


async def test_claude_code_runner_errors():
    bad = ScriptedSandbox([cli("x", is_error=True, subtype="error_during_execution")])
    with pytest.raises(AgentError, match="Claude Code error"):
        await ClaudeCodeRunner(bad, "t", get_settings()).run(agent="a", prompt="p", schema=FixResult, tools=[], edit=False)
    garbage = ScriptedSandbox(["command not found"])
    with pytest.raises(AgentError, match="no result"):
        await ClaudeCodeRunner(garbage, "t", get_settings()).run(agent="a", prompt="p", schema=FixResult, tools=[], edit=False)
    never = ScriptedSandbox([cli("nope"), cli("still nope")])
    with pytest.raises(AgentError, match="did not match"):
        await ClaudeCodeRunner(never, "t", get_settings()).run(agent="a", prompt="p", schema=FixResult, tools=[], edit=False)


# ---------------- Anthropic LLM wrapper (SDK against a mock transport) ----------------

def llm_with(handler) -> AnthropicLLM:
    import anthropic
    import httpx2  # the Anthropic SDK 2.x transport package

    llm = AnthropicLLM(get_settings())
    llm.client = anthropic.AsyncAnthropic(api_key="k", base_url="https://api.anthropic.test", max_retries=0,
                                          http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(
                                              lambda req: (lambda r: httpx2.Response(r.status_code, content=r.content, headers=dict(r.headers)))(
                                                  handler(httpx.Request(req.method, str(req.url), headers=dict(req.headers), content=req.read()))))))
    return llm


def message(text: str, stop="end_turn") -> dict:
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "stop_reason": stop,
            "stop_sequence": None, "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 1000, "output_tokens": 100}}


async def test_llm_structured_output_and_request_shape():
    seen = {}

    def handler(req):
        seen["body"], seen["beta"] = json.loads(req.content), req.headers.get("anthropic-beta", "")
        return httpx.Response(200, json=message(json.dumps({"verdict": "approve", "summary": "ok", "issues": []})))

    res = await llm_with(handler).structured(agent="review", system="sys", prompt="diff", schema=ReviewVerdict)
    assert res.parsed.verdict == "approve" and res.tokens_in == 1000 and res.cost_usd == pytest.approx(0.006)
    assert seen["body"]["model"] == "claude-opus-5-5" and seen["body"]["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in seen["beta"]
    assert "temperature" not in seen["body"] and "tool_choice" not in seen["body"]
    fmt = seen["body"]["output_config"]["format"]
    assert fmt["type"] == "json_schema" and fmt["schema"]["additionalProperties"] is False


async def test_llm_refusal_and_errors():
    with pytest.raises(AgentError, match="declined"):
        await llm_with(lambda r: httpx.Response(200, json=message("", stop="refusal"))).structured(
            agent="review", system="s", prompt="p", schema=ReviewVerdict)
    with pytest.raises(AgentError, match="did not match"):
        await llm_with(lambda r: httpx.Response(200, json=message('{"verdict": "maybe"}'))).structured(
            agent="review", system="s", prompt="p", schema=ReviewVerdict)
    with pytest.raises(AgentError, match="rate limited"):
        await llm_with(lambda r: httpx.Response(429, json={"type": "error", "error": {"type": "rate_limit_error", "message": "slow"}})).structured(
            agent="review", system="s", prompt="p", schema=ReviewVerdict)


# ---------------- LLM gateway ----------------

_seq = iter(range(1, 10**6))


async def make_token(*, revoked=False, expired=False, requests=0, tokens=0) -> str:
    n = next(_seq)
    async with SessionLocal() as db:
        from tests.fake_github import installation
        from app.github.sync import upsert_installation, upsert_repo
        inst = await upsert_installation(db, installation())
        repo = await upsert_repo(db, inst, {"id": 1, "full_name": "r/x", "private": True})
        bug = Bug(repository_id=repo.id, source="github_issue", title="t", severity="low", status="new", fingerprint=f"f{n}", affected_files=[], data={})
        db.add(bug)
        await db.flush()
        run = WorkflowRun(bug_id=bug.id, repository_id=repo.id, status="running", nodes={})
        db.add(run)
        await db.flush()
        raw = f"dgw_test_{n}"
        db.add(GatewayToken(token_hash=hashlib.sha256(raw.encode()).hexdigest(), run_id=run.id, request_count=requests,
                            tokens_in=tokens, expires_at=datetime.now(UTC) + timedelta(hours=-1 if expired else 1),
                            revoked_at=datetime.now(UTC) if revoked else None))
        await db.commit()
        return raw


@pytest.fixture
def upstream(monkeypatch):
    calls = []

    def handler(req):
        calls.append(req)
        body = json.loads(req.content)
        if body.get("stream"):
            sse = ('event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":120,"cache_read_input_tokens":30}}}\n\n'
                   'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"type":"text_delta","text":"hi"}}\n\n'
                   'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":45}}\n\n'
                   'event: message_stop\ndata: {"type":"message_stop"}\n\n')
            return httpx.Response(200, content=sse.encode(), headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json=message("ok"))

    monkeypatch.setattr(gateway, "_client", httpx.AsyncClient(base_url="https://api.anthropic.test", transport=httpx.MockTransport(handler)))
    return calls


def gw_client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway.app), base_url="http://llm-gateway:8090")


async def test_gateway_swaps_key_streams_and_accounts(upstream, db):
    raw = await make_token()
    body = {"model": "claude-opus-5-5", "max_tokens": 10, "stream": True, "messages": [{"role": "user", "content": "x"}]}
    async with gw_client() as c:
        r = await c.post("/v1/messages?beta=true", json=body, headers={"x-api-key": raw, "anthropic-version": "2023-06-01"})
        assert r.status_code == 200 and "message_stop" in r.text
        r2 = await c.post("/v1/messages", json={**body, "stream": False}, headers={"authorization": f"Bearer {raw}"})
        assert r2.status_code == 200
    sent = upstream[0]
    assert sent.headers["x-api-key"] == get_settings().anthropic_api_key  # real key added by the gateway
    assert raw not in json.dumps(dict(sent.headers))  # run token never forwarded upstream
    assert sent.url.params.get("beta") == "true"
    tok = (await db.execute(select(GatewayToken))).scalar_one()
    await db.refresh(tok)
    assert tok.request_count == 2 and tok.tokens_in == 150 + 1000 and tok.tokens_out == 45 + 100


async def test_gateway_rejections(upstream, db, monkeypatch):
    body = {"model": "claude-opus-5-5", "max_tokens": 10, "messages": []}
    async with gw_client() as c:
        assert (await c.post("/v1/messages", json=body)).status_code == 401
        assert (await c.post("/v1/messages", json=body, headers={"x-api-key": "dgw_bogus"})).status_code == 401
        for kw in ({"revoked": True}, {"expired": True}):
            raw = await make_token(**kw)
            assert (await c.post("/v1/messages", json=body, headers={"x-api-key": raw})).status_code == 401
    monkeypatch.setattr(get_settings(), "run_max_llm_requests", 5)
    raw = await make_token(requests=5)
    async with gw_client() as c:
        r = await c.post("/v1/messages", json=body, headers={"x-api-key": raw})
        assert r.status_code == 429 and "budget" in r.json()["error"]["message"]
    raw = await make_token()
    async with gw_client() as c:
        assert (await c.post("/v1/messages", json={**body, "model": "some-other-model"}, headers={"x-api-key": raw})).status_code == 400
        assert (await c.post("/v1/files", json=body, headers={"x-api-key": raw})).status_code == 404
        assert (await c.get("/v1/models", headers={"x-api-key": raw})).status_code == 404
    assert upstream == []  # nothing rejected ever reached the Claude API
