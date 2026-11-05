"""AWS DevOps Agent MCP integration: connectivity, auth failures, tool discovery, tool calls, throttling backoff."""

import pytest

from app.config import get_settings
from app.integrations import devops_agent as da
from tests.fake_devops_agent import TOKEN, FakeDevOpsAgent


@pytest.fixture
def agent(monkeypatch):
    fake = FakeDevOpsAgent()
    url = fake.start()
    s = get_settings()
    monkeypatch.setattr(s, "devops_agent_url", url)
    monkeypatch.setattr(s, "devops_agent_token", TOKEN)
    yield fake
    fake.stop()


async def test_connectivity_and_tool_discovery(agent):
    res = await da.check_connectivity(da.DevOpsAgentClient(get_settings()))
    assert res["ok"] is True and res["missing_tools"] == [] and res["error"] is None
    assert {"investigate", "get_task", "list_journal_records", "list_recommendations"} <= set(res["tools"])
    assert TOKEN not in str(res)  # the check result is stored/displayed: never contains the token


async def test_invalid_token_is_an_auth_error(agent, monkeypatch):
    monkeypatch.setattr(get_settings(), "devops_agent_token", "wrong-token")
    res = await da.check_connectivity(da.DevOpsAgentClient(get_settings()))
    assert res["ok"] is False and "401" in res["error"] and "wrong-token" not in res["error"]
    with pytest.raises(da.DevOpsAgentAuthError):
        await da.DevOpsAgentClient(get_settings()).list_tools()


async def test_unreachable_endpoint(monkeypatch):
    monkeypatch.setattr(get_settings(), "devops_agent_url", "http://127.0.0.1:9/mcp")  # nothing listens on port 9
    monkeypatch.setattr(get_settings(), "devops_agent_token", TOKEN)
    with pytest.raises(da.DevOpsAgentUnavailable):
        await da.DevOpsAgentClient(get_settings()).list_tools()


async def test_not_configured(monkeypatch):
    monkeypatch.setattr(get_settings(), "devops_agent_token", "")
    with pytest.raises(da.DevOpsAgentNotConfigured):
        await da.DevOpsAgentClient(get_settings()).list_tools()


async def test_missing_required_tools_fails_check(agent, monkeypatch):
    monkeypatch.setattr(da, "REQUIRED_TOOLS", da.REQUIRED_TOOLS | {"tool_that_does_not_exist"})
    res = await da.check_connectivity(da.DevOpsAgentClient(get_settings()))
    assert res["ok"] is False and res["missing_tools"] == ["tool_that_does_not_exist"]


async def test_investigation_tool_calls_and_shapes(agent):
    c = da.DevOpsAgentClient(get_settings())
    started = await c.call("investigate", {"title": "5xx on login since deploy abc1234"})
    assert started["status"] == "investigation_started" and started["taskId"] == "task-123" and started["executionId"] == "exec-456"
    task = await c.call("get_task", {"task_id": "task-123"})
    assert task["task"]["status"] == "IN_PROGRESS"
    journal = await c.call("list_journal_records", {"execution_id": "exec-456", "order": "ASC"})
    assert journal["records"][1]["recordType"] == "FINDING" and journal["next_token"] == "page-2"
    recs = await c.call("list_recommendations", {"task_id": "task-123"})
    assert recs["recommendations"][0]["recommendationId"] == "rec-1"


async def test_throttling_backs_off_then_succeeds(agent, monkeypatch):
    waits = []

    async def fake_sleep(seconds):
        waits.append(seconds)
    agent.throttle_next = 2
    task = await da.DevOpsAgentClient(get_settings(), sleep=fake_sleep).call("get_task", {"task_id": "task-123"})
    assert task["task"]["taskId"] == "task-123" and waits == [60, 90]  # AWS guidance: 60s, then 90s, then 120s


async def test_tool_errors_are_errors_not_results(agent):
    agent.throttle_next = 10  # keeps throttling: eventually surfaces as an error, never as a fake result

    async def no_sleep(_):
        return None
    with pytest.raises(da.DevOpsAgentThrottled):
        await da.DevOpsAgentClient(get_settings(), sleep=no_sleep).call("get_task", {"task_id": "x"}, retries=1)
    with pytest.raises(da.DevOpsAgentToolError):
        await da.DevOpsAgentClient(get_settings()).call("no_such_tool", {})


# ---------------- read-only MCP proxy in llm-gateway (what Claude Code inside a sandbox talks to) ----------------

async def _proxy_session_tools(run_token: str):
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    from app import gateway
    http = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=gateway.app), headers={"Authorization": f"Bearer {run_token}"}, timeout=30)
    async with http, streamable_http_client("http://llm-gateway:8090/mcp/aws-devops-agent", http_client=http) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = sorted(t.name for t in (await s.list_tools()).tools)
            task = await s.call_tool("get_task", {"task_id": "task-123"})
            blocked = []
            for name, args in (("investigate", {"title": "x"}), ("create_access_token", {"name": "x"})):
                try:
                    res = await s.call_tool(name, args)
                    blocked.append(bool(getattr(res, "is_error", False)))
                except Exception:
                    blocked.append(True)
            return tools, task, blocked


async def test_gateway_proxy_is_read_only_and_keeps_the_real_token_out(agent, db):
    from tests.test_agents import make_token
    run_token = await make_token()
    tools, task, blocked = await _proxy_session_tools(run_token)
    assert tools == ["get_recommendation", "get_task", "list_journal_records", "list_recommendations"]  # admin/write tools hidden
    assert blocked == [True, True]  # cannot start investigations or create AWS access tokens
    assert "create_access_token" not in [c[0] for c in agent.calls] and "investigate" not in [c[0] for c in agent.calls]
    assert any(c[0] == "get_task" for c in agent.calls)  # read-only call went through
    assert run_token not in " ".join(agent.auth_headers)  # sandbox token never reaches AWS
    assert all(h == f"Bearer {TOKEN}" for h in agent.auth_headers)  # real token injected by the gateway only


async def test_gateway_proxy_rejects_bad_run_tokens(agent):
    import httpx2

    from app import gateway
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=gateway.app)) as c:
        r = await c.post("http://gw/mcp/aws-devops-agent", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                         headers={"Authorization": "Bearer dgw_bogus"})
        assert r.status_code == 401
        assert (await c.get("http://gw/mcp/aws-devops-agent")).status_code == 404  # no server-initiated streams
    assert agent.auth_headers == []  # nothing reached the upstream


# ---------------- Claude Code configuration ----------------

async def test_claude_code_gets_proxy_config_never_the_real_token(agent):
    from app.agents.claude_code import ClaudeCodeRunner
    from app.agents.schemas import FixResult
    from tests.test_agents import ScriptedSandbox, cli
    sb = ScriptedSandbox([cli('```json\n{"summary": "s", "rationale": "r", "files_changed": []}\n```')])
    await ClaudeCodeRunner(sb, "dgw_run_tok", get_settings()).run(agent="investigation", prompt="p", schema=FixResult,
                                                                  tools=["Read"], edit=False, devops_agent=True)
    cmd, env = sb.calls[0]
    cfg = cmd[cmd.index("--mcp-config") + 1]
    assert "/mcp/aws-devops-agent" in cfg and "dgw_run_tok" in cfg and TOKEN not in cfg and TOKEN not in str(env)
    allowed = cmd[cmd.index("--allowedTools") + 1]
    assert "mcp__aws-devops-agent__get_task" in allowed and "mcp__aws-devops-agent__investigate" not in allowed
    assert "--strict-mcp-config" in cmd  # no other MCP servers can be picked up from the repository


# ---------------- integration status / gating API ----------------

async def test_integration_check_and_enable_gating(agent, monkeypatch):
    from app.models import Role
    from tests.conftest import logged_in
    async with logged_in(Role.reviewer) as rev:
        assert (await rev.post("/api/v1/integrations/aws-devops-agent/check")).status_code == 403
        assert (await rev.get("/api/v1/integrations/aws-devops-agent")).json()["connected"] is False
    async with logged_in(Role.admin) as admin:
        assert (await admin.patch("/api/v1/integrations/aws-devops-agent", json={"enabled": True})).status_code == 409  # no check yet
        res = (await admin.post("/api/v1/integrations/aws-devops-agent/check")).json()
        assert res["connected"] is True and "investigate" in res["last_check"]["tools"] and TOKEN not in str(res)
        assert (await admin.patch("/api/v1/integrations/aws-devops-agent", json={"enabled": True})).json()["enabled"] is True
        monkeypatch.setattr(get_settings(), "devops_agent_token", "revoked-token")
        res = (await admin.post("/api/v1/integrations/aws-devops-agent/check")).json()
        assert res["connected"] is False and res["enabled"] is False  # a failing check switches the workflow off


# ---------------- Phase 3: investigation service ----------------

from app.integrations import investigation as inv  # noqa: E402


async def _no_sleep(_):
    return None


async def test_investigation_runs_to_completion_streaming_records(agent):
    seen, statuses = [], []

    async def on_record(r):
        seen.append(r)

    async def on_status(st, task_id, exec_id):
        statuses.append(st)
    res = await inv.run_investigation(da.DevOpsAgentClient(get_settings()), "5xx on login since deploy abc1234",
                                      on_record=on_record, on_status=on_status, poll_seconds=0, sleep=_no_sleep)
    assert res.status == "COMPLETED" and res.task_id == "task-123" and res.execution_id == "exec-456"
    assert statuses == ["CREATED", "IN_PROGRESS", "COMPLETED"]
    assert [r["type"] for r in seen][:2] == ["SEARCHING", "FINDING"] and any(r["type"] == "SUMMARY" for r in seen)
    pages = [kw for name, kw in agent.calls if name == "list_journal_records" and kw["order"] == "ASC"]
    assert pages[0]["next_token"] is None and pages[1]["next_token"] == "page-2"  # only new records each poll
    assert "Root cause" in res.summary and "SEARCHING" not in res.summary
    assert res.recommendations[0]["recommendationId"] == "rec-1"
    assert agent.calls[0] == ("investigate", {"title": "5xx on login since deploy abc1234"})


async def test_failed_investigation_keeps_partial_findings(monkeypatch):
    fake = FakeDevOpsAgent(fail=True)
    url = fake.start()
    monkeypatch.setattr(get_settings(), "devops_agent_url", url)
    monkeypatch.setattr(get_settings(), "devops_agent_token", TOKEN)
    try:
        res = await inv.run_investigation(da.DevOpsAgentClient(get_settings()), "x", poll_seconds=0, sleep=_no_sleep)
    finally:
        fake.stop()
    assert res.status == "FAILED" and res.records and "partial" in res.error and res.recommendations == []
    assert "list_recommendations" not in [c[0] for c in fake.calls]


async def test_investigation_timeout_returns_partial(agent):
    agent.polls_until_done = 10_000  # never completes
    t = [0.0]

    def clock():
        t[0] += 60  # each poll "takes" a minute
        return t[0]
    res = await inv.run_investigation(da.DevOpsAgentClient(get_settings()), "x", poll_seconds=0, max_minutes=3,
                                      sleep=_no_sleep, clock=clock)
    assert res.status == "TIMED_OUT" and "partial" in res.error and res.records


async def test_investigation_start_failure_is_reported(monkeypatch, agent):
    monkeypatch.setattr(get_settings(), "devops_agent_token", "bad")
    res = await inv.run_investigation(da.DevOpsAgentClient(get_settings()), "x", poll_seconds=0, sleep=_no_sleep)
    assert res.status == "ERROR" and "401" in res.error


def test_record_normalization_accepts_common_shapes():
    assert inv.normalize_record({"recordType": "finding", "content": "x"})["type"] == "FINDING"
    assert inv.normalize_record({"type": "SUMMARY", "text": "y"})["text"] == "y"
    assert "weird" in inv.normalize_record({"weird": 1})["text"]  # unknown shape kept verbatim, never dropped


# ---------------- Phase 5: classification rules ----------------

def test_classification_rules():
    from app.workflow.classify import classify_by_rules
    assert classify_by_rules("aws_alarm", [])[0] == "incident"
    assert classify_by_rules("auto_health", [])[0] == "incident"
    assert classify_by_rules("github_issue", ["Bug", "Production"])[0] == "incident"
    assert classify_by_rules("github_issue", ["enhancement"])[0] == "feature"
    assert classify_by_rules("auto_semgrep", [])[0] == "bug"
    assert classify_by_rules("github_issue", ["bug"]) is None  # ambiguous: the AI decides


# ---------------- Phase 5: classification rules ----------------

def test_classification_rules():
    from app.workflow.classify import classify_by_rules
    assert classify_by_rules("aws_alarm", [])[0] == "incident"
    assert classify_by_rules("auto_health", [])[0] == "incident"
    assert classify_by_rules("github_issue", ["Bug", "Production"])[0] == "incident"
    assert classify_by_rules("github_issue", ["enhancement"])[0] == "feature"
    assert classify_by_rules("auto_semgrep", [])[0] == "bug"
    assert classify_by_rules("github_issue", ["bug"]) is None  # ambiguous: the AI decides
