"""CloudWatch alarms -> SNS -> DebugOps. Public endpoint; every message is SNS-signature-verified."""

import fnmatch
import json
import logging
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import audit
from app.config import get_settings
from app.db import get_db
from app.detection.parsers import fingerprint, redact
from app.events import log_event
from app.integrations import sns
from app.models import Bug, Evidence, GitHubInstallation, Repository, WebhookDelivery
from app.schemas import RepoSettings

log = logging.getLogger("debugops.alerts")
router = APIRouter(tags=["alerts"])
Db = Annotated[AsyncSession, Depends(get_db)]
OPEN = {"new", "triaged", "investigating", "fix_proposed", "pr_open"}


def _alarm_text(a: dict) -> str:
    t = a.get("Trigger") or {}
    dims = ", ".join(f"{d.get('name')}={d.get('value')}" for d in t.get("Dimensions") or [])
    return (f"CloudWatch alarm {a.get('AlarmName')} entered ALARM at {a.get('StateChangeTime')} "
            f"(account {a.get('AWSAccountId')}, {a.get('Region')}).\n"
            f"Reason: {a.get('NewStateReason')}\n"
            f"Description: {a.get('AlarmDescription') or '-'}\n"
            f"Metric: {t.get('Namespace')}/{t.get('MetricName')} {dims}")


async def _repo_for_alarm(db: AsyncSession, alarm_name: str) -> Repository | None:
    rows = (await db.execute(select(Repository).join(GitHubInstallation, GitHubInstallation.id == Repository.installation_id)
                             .where(Repository.enabled.is_(True), Repository.accessible.is_(True),
                                    GitHubInstallation.deleted_at.is_(None)).order_by(Repository.id))).scalars().all()
    for repo in rows:
        if any(fnmatch.fnmatchcase(alarm_name, p) for p in RepoSettings.of(repo.settings).alarm_patterns):
            return repo
    return None


async def handle_alarm(db: AsyncSession, alarm: dict) -> dict:
    """One CloudWatch alarm notification -> incident bug (deduplicated) -> optional automatic run."""
    from app.workflow.runner import start_run
    name = str(alarm.get("AlarmName") or "")
    if alarm.get("NewStateValue") != "ALARM":
        return {"status": "ignored", "reason": f"state {alarm.get('NewStateValue')}"}
    repo = await _repo_for_alarm(db, name)
    if repo is None:
        log_event(db, "alerts", f"CloudWatch alarm {name!r} matched no repository (set alarm patterns in repository settings)",
                  level="warning", type="error")
        return {"status": "ignored", "reason": "no repository matches this alarm"}
    fp = fingerprint("aws_alarm", name, str(alarm.get("AWSAccountId")), str(alarm.get("Region")))
    bug = (await db.execute(select(Bug).where(Bug.repository_id == repo.id, Bug.fingerprint == fp))).scalar_one_or_none()
    text = redact(_alarm_text(alarm))
    start = False
    if bug is None:
        bug = Bug(repository_id=repo.id, source="aws_alarm", fingerprint=fp, severity="high", status="new",
                  title=f"Production alarm: {name}"[:500], description=text, affected_files=[],
                  data={"alarm": name, "region": alarm.get("Region"), "account": alarm.get("AWSAccountId"),
                        "since": alarm.get("StateChangeTime"), "labels": ["incident"]})
        db.add(bug)
        await db.flush()
        start = True
    else:
        bug.occurrences += 1
        if bug.status not in OPEN:  # fixed before and firing again: reopen
            bug.status, start = "new", True
    db.add(Evidence(bug_id=bug.id, kind="log", content=redact(json.dumps(alarm, indent=1, default=str))[:20000]))
    log_event(db, "alerts", f"CloudWatch alarm {name} fired for {repo.full_name}: bug #{bug.id}" + ("" if start else " (already being handled)"),
              level="warning")
    await db.commit()
    run_id = None
    s = get_settings()
    if start and RepoSettings.of(repo.settings).auto_investigate_alarms and s.agents_configured:
        run = await start_run(db, bug, None)
        run_id = run.id if run else None
    return {"status": "processed", "bug_id": bug.id, "run_id": run_id}


@router.post("/aws/sns")
async def sns_endpoint(request: Request, db: Db):
    s = get_settings()
    if not s.sns_topic_arns:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "SNS endpoint is not enabled")
    try:
        msg = json.loads(await request.body())
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "body is not JSON") from e
    try:
        sns.verify(msg)
    except sns.SNSVerificationError as e:
        log.warning("rejected SNS message: %s", e)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"SNS verification failed: {e}") from e
    if msg.get("TopicArn") not in s.sns_topic_arns:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "topic not allowed")

    inserted = await db.scalar(insert(WebhookDelivery).values(
        delivery_id=f"sns:{msg.get('MessageId')}", event="sns", action=msg.get("Type"), status="received",
        payload={k: v for k, v in msg.items() if k not in ("Signature",)}).on_conflict_do_nothing(
        index_elements=[WebhookDelivery.delivery_id]).returning(WebhookDelivery.id))
    if inserted is None:
        return {"status": "duplicate"}
    await db.commit()

    if msg.get("Type") == "SubscriptionConfirmation":
        url = msg.get("SubscribeURL", "")
        if not sns.is_sns_url(url):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "SubscribeURL is not an SNS endpoint")
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as c:
            (await c.get(url)).raise_for_status()
        audit(db, "sns.subscription_confirmed", actor_type="system", target_type="sns_topic", target_id=msg.get("TopicArn"))
        result = {"status": "subscribed"}
    elif msg.get("Type") == "Notification":
        try:
            alarm = json.loads(msg.get("Message") or "")
        except ValueError:
            alarm = None
        result = await handle_alarm(db, alarm) if isinstance(alarm, dict) and alarm.get("AlarmName") else \
            {"status": "ignored", "reason": "not a CloudWatch alarm"}
    else:
        result = {"status": "ignored", "reason": msg.get("Type")}
    row = await db.get(WebhookDelivery, inserted)
    row.status = result["status"]
    await db.commit()
    return result
