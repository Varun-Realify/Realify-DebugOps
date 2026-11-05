from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app import auth
from app.audit import audit
from app.config import get_settings
from app.db import get_db
from app.events import log_event
from app.integrations.devops_agent import DevOpsAgentClient, check_connectivity
from app.models import Integration, Role

router = APIRouter(tags=["integrations"])
Db = Annotated[AsyncSession, Depends(get_db)]
Admin = Annotated[auth.Principal, auth.require_role(Role.admin)]
NAME = "aws-devops-agent"


class EnableIn(BaseModel):
    enabled: bool


async def _row(db: AsyncSession) -> Integration:
    row = await db.get(Integration, NAME)
    if row is None:
        row = Integration(name=NAME, enabled=False, last_check={})
        db.add(row)
        await db.flush()
    return row


def _out(row: Integration) -> dict:
    s = get_settings()
    return {"name": NAME, "configured": s.devops_agent_configured, "auth": s.devops_agent_auth,
            "region": s.devops_agent_region, "endpoint": s.devops_agent_endpoint, "enabled": row.enabled,
            "connected": bool(row.last_check.get("ok")), "last_check": row.last_check, "checked_at": row.checked_at}


@router.get("/integrations/aws-devops-agent")
async def devops_agent_status(_: auth.CurrentPrincipal, db: Db):
    row = await _row(db)
    await db.commit()
    return _out(row)


@router.post("/integrations/aws-devops-agent/check")
async def devops_agent_check(p: Admin, request: Request, db: Db):
    """Live connectivity test (MCP initialize + tools/list) against the configured endpoint. Never returns the token."""
    result = await check_connectivity(DevOpsAgentClient(get_settings()))
    row = await _row(db)
    row.last_check, row.checked_at = result, datetime.now(UTC)
    if not result["ok"] and row.enabled:
        row.enabled = False  # a failing integration is never left powering production workflows
    audit(db, "integration.checked", actor_user_id=p.user.id, target_type="integration", target_id=NAME,
          ip=auth.client_ip(request), ok=result["ok"], error=result["error"])
    log_event(db, "integrations", f"AWS DevOps Agent connectivity check: {'OK' if result['ok'] else 'FAILED - ' + str(result['error'])}",
              level="info" if result["ok"] else "error", type="info" if result["ok"] else "error")
    await db.commit()
    return _out(row)


@router.patch("/integrations/aws-devops-agent")
async def devops_agent_enable(body: EnableIn, p: Admin, request: Request, db: Db):
    row = await _row(db)
    if body.enabled and not (get_settings().devops_agent_configured and row.last_check.get("ok")):
        raise HTTPException(status.HTTP_409_CONFLICT, "Run a successful connectivity check before enabling the incident workflow")
    row.enabled = body.enabled
    audit(db, "integration.enabled" if body.enabled else "integration.disabled", actor_user_id=p.user.id,
          target_type="integration", target_id=NAME, ip=auth.client_ip(request))
    await db.commit()
    return _out(row)
