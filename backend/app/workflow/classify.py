"""Classify a task as a production incident, an ordinary bug, or a feature request.

Deterministic rules first (source, labels); the AI is asked only when the rules have no answer. Only incidents are
sent to AWS DevOps Agent.
"""

from typing import Literal

from pydantic import BaseModel

from app.agents import prompts

INCIDENT_LABELS = {"incident", "production", "prod", "outage", "sev0", "sev1", "sev2", "p0", "p1", "hotfix", "alarm",
                   "production-incident", "prod-incident"}
FEATURE_LABELS = {"enhancement", "feature", "feature-request", "feature request", "new feature"}
INCIDENT_SOURCES = {"aws_alarm", "auto_health"}

TaskType = Literal["incident", "bug", "feature"]


class TaskClass(BaseModel):
    type: TaskType
    reason: str


SYSTEM = prompts.SHARED_RULES + """
Role: classify a development task.
- incident: a problem happening in PRODUCTION (users affected, errors/5xx, outages, latency, crashes, alarms) where
  runtime evidence such as logs, metrics or deployment history would help find the cause.
- bug: incorrect behaviour that can be understood from the code and tests alone (not an active production problem).
- feature: a request for new or changed behaviour.
Pick exactly one. Prefer bug when unsure between bug and incident."""


def classify_by_rules(source: str, labels: list[str]) -> tuple[TaskType, str] | None:
    names = {lbl.lower().strip() for lbl in labels}
    if source in INCIDENT_SOURCES:
        return "incident", f"source {source} is a production signal"
    if names & INCIDENT_LABELS:
        return "incident", f"label {sorted(names & INCIDENT_LABELS)[0]!r}"
    if names & FEATURE_LABELS:
        return "feature", f"label {sorted(names & FEATURE_LABELS)[0]!r}"
    if source.startswith("auto_"):
        return "bug", f"detected by scan ({source})"
    return None


async def classify(bug: dict, llm_factory) -> tuple[TaskType, str, dict | None]:
    """Returns (type, reason, llm_usage). llm_factory is None when AI agents are not configured."""
    rule = classify_by_rules(bug["source"], (bug.get("data") or {}).get("labels") or [])
    if rule:
        return rule[0], rule[1], None
    if llm_factory is None:
        return "bug", "no matching label; AI classification unavailable", None
    res = await llm_factory().structured(agent="classifier", system=SYSTEM, prompt=prompts.bug_block(bug), schema=TaskClass)
    return res.parsed.type, f"AI: {res.parsed.reason}"[:300], {"in": res.tokens_in, "out": res.tokens_out, "cost": res.cost_usd}
