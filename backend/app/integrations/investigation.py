"""Run one AWS DevOps Agent investigation end to end, following AWS's official procedure
(plugins/aws-agents-for-devsecops/skills/investigating-incidents-with-aws-devops-agent):

  investigate(title) -> taskId, executionId
  every >=30s: get_task(task_id) for status; list_journal_records(execution_id, order=ASC, next_token) for NEW records
  COMPLETED: list_journal_records(order=DESC, limit=10) for the final findings; list_recommendations(task_id)
  FAILED / TIMED_OUT / CANCELED / SKIPPED / LINKED: journal may still hold partial findings -> keep them
  no completion within max time -> TIMED_OUT with partial findings

Everything returned is untrusted text produced by the agent. It is stored and displayed, and handed to Claude Code
as data; nothing in it is ever executed.
"""

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.integrations.devops_agent import DevOpsAgentClient, DevOpsAgentError

# Task statuses that end an investigation (AWS DevOps Agent API TaskStatus enum). PENDING_*, IN_PROGRESS and WAITING
# are still running. LINKED: triage attached the alert to an existing investigation; SKIPPED: triage chose not to run.
TERMINAL = {"COMPLETED", "FAILED", "TIMED_OUT", "CANCELED", "SKIPPED", "LINKED"}
KEY_TYPES = {"FINDING", "SUMMARY", "SUGGESTION"}
MAX_RECORDS = 500
MAX_TEXT = 4000


def normalize_record(r: dict) -> dict:
    """Journal record -> {type, text}. Field names are not documented by AWS, so accept the common spellings."""
    rtype = str(r.get("recordType") or r.get("record_type") or r.get("type") or "RECORD").upper()
    text = r.get("content") or r.get("text") or r.get("message") or r.get("summary")
    if isinstance(text, dict):  # API: content is a JSON document
        text = next((text[k] for k in ("text", "summary", "markdown", "message") if isinstance(text.get(k), str)), text)
    if not isinstance(text, str):
        text = json.dumps(text if text is not None else r, default=str)
    return {"type": rtype, "text": text[:MAX_TEXT], "at": r.get("createdAt") or r.get("timestamp") or datetime.now(UTC).isoformat()}


def investigation_request(*, title: str, repository: str, branch: str, severity: str, started_at: str,
                          description: str, evidence: str, alarm: dict | None = None) -> str:
    """The text sent to `investigate(title=...)`. AWS's skill packs all context into this one field; the console asks
    for the same things: what is wrong, a starting point (alarm, metric or log), and when it started (UTC)."""
    lines = [f"Production incident: {title}",
             f"Started: {started_at} (UTC). Severity: {severity}.",
             f"Service code: GitHub repository {repository}, deployed from branch {branch}."]
    if alarm:
        lines.append(f"Starting point: CloudWatch alarm {alarm.get('alarm')!r} in region {alarm.get('region')}, "
                     f"account {alarm.get('account')}.")
    lines += ["", "What was observed:", description.strip()[:3000] or "(no description)"]
    if evidence.strip():
        lines += ["", "Evidence (stack traces, logs, alarm payload):", evidence.strip()[:5000]]
    lines += ["", "Please determine: (1) the root cause; (2) whether a recent deployment or code change caused it, naming "
              "the commit, deployment and source files involved; (3) the affected resources; (4) mitigation steps. "
              "A coding agent will use your findings to fix the code in the repository above."]
    return "\n".join(lines)


def recommendation_brief(r: dict) -> dict:
    """Keep what a coding agent needs from a recommendation (API fields: title, content{summary, spec}, priority)."""
    content = r.get("content") if isinstance(r.get("content"), dict) else {}
    return {"recommendationId": r.get("recommendationId"), "title": str(r.get("title") or "")[:300],
            "priority": r.get("priority"), "summary": str(content.get("summary") or r.get("summary") or "")[:1500],
            "spec": str(content.get("spec") or "")[:4000]}


@dataclass
class InvestigationResult:
    status: str
    task_id: str | None = None
    execution_id: str | None = None
    records: list[dict] = field(default_factory=list)
    recommendations: list[dict] = field(default_factory=list)
    error: str | None = None

    @property
    def summary(self) -> str:
        """Final summary + key findings (what Claude Code receives)."""
        key = [r for r in self.records if r["type"] in KEY_TYPES]
        return "\n\n".join(f"[{r['type']}] {r['text']}" for r in key)[-20000:]


async def run_investigation(client: DevOpsAgentClient, title: str, *,
                            on_record: Callable[[dict], Awaitable[None]] | None = None,
                            on_status: Callable[[str, str | None, str | None], Awaitable[None]] | None = None,
                            poll_seconds: float = 30, max_minutes: float = 20,
                            sleep=asyncio.sleep, clock=time.monotonic) -> InvestigationResult:
    poll_seconds = max(poll_seconds, 30) if sleep is asyncio.sleep else poll_seconds  # AWS: never faster than 30s
    res = InvestigationResult(status="pending")

    async def emit(record: dict):
        if len(res.records) < MAX_RECORDS:
            res.records.append(record)
            if on_record:
                await on_record(record)

    try:
        started = await client.call("investigate", {"title": title[:20000]})
    except DevOpsAgentError as e:
        return InvestigationResult(status="ERROR", error=str(e))
    res.task_id, res.execution_id = started.get("taskId"), started.get("executionId")
    if not res.task_id:
        return InvestigationResult(status="ERROR", error=f"unexpected investigate response: {str(started)[:300]}")
    res.status = "CREATED"
    if on_status:
        await on_status(res.status, res.task_id, res.execution_id)

    deadline = clock() + max_minutes * 60
    next_token: str | None = None
    while True:
        await sleep(poll_seconds)
        try:
            task = (await client.call("get_task", {"task_id": res.task_id})).get("task") or {}
            status = str(task.get("status") or res.status).upper()
            res.execution_id = res.execution_id or task.get("executionId")
            if status != res.status:
                res.status = status
                if on_status:
                    await on_status(status, res.task_id, res.execution_id)
            if res.execution_id:
                args = {"execution_id": res.execution_id, "order": "ASC"}
                if next_token:
                    args["next_token"] = next_token
                page = await client.call("list_journal_records", args)
                for r in page.get("records") or []:
                    await emit(normalize_record(r))
                next_token = page.get("next_token") or page.get("nextToken") or next_token
        except DevOpsAgentError as e:
            # Transient problems while polling: note them and keep going until the deadline.
            await emit({"type": "ERROR", "text": f"polling problem: {e}", "at": datetime.now(UTC).isoformat()})
        if res.status in TERMINAL:
            break
        if clock() >= deadline:
            res.status, res.error = "TIMED_OUT", f"no completion within {max_minutes:g} minutes; partial findings kept"
            return res

    try:
        if res.execution_id:  # final findings (newest first), in case the paged loop missed the tail
            final = await client.call("list_journal_records", {"execution_id": res.execution_id, "order": "DESC", "limit": 10})
            seen = {(r["type"], r["text"]) for r in res.records}
            for r in reversed(final.get("records") or []):
                n = normalize_record(r)
                if (n["type"], n["text"]) not in seen:
                    await emit(n)
        if res.status == "COMPLETED":
            recs = await client.call("list_recommendations", {"task_id": res.task_id})
            res.recommendations = [r for r in (recs.get("recommendations") or []) if isinstance(r, dict)][:20]
    except DevOpsAgentError as e:
        res.error = f"could not fetch final findings: {e}"
    if res.status != "COMPLETED" and not res.error:
        res.error = f"the investigation ended {res.status}; partial findings (if any) are kept"
    return res
