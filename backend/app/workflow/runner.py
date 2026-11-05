"""Start, execute and resume debugging runs. State is checkpointed in PostgreSQL after every node, so a run
survives worker restarts and can wait days for human approval."""

import logging
import re
import time
from datetime import UTC, datetime

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import queue
from app.config import get_settings
from app.db import SessionLocal
from app.events import log_event
from app.models import Bug, Repository, WorkflowRun
from app.workflow.graph import Deps, RunCancelled, build_graph, release_workspace, release_worktree, revoke_gateway_tokens

log = logging.getLogger("debugops.runner")
TERMINAL = {"succeeded", "needs_human", "failed", "rejected", "cancelled", "closed"}


def task_id(run_id: int) -> str:
    return f"DBG-{run_id:03d}"


def task_branch(run_id: int, title: str) -> str:
    """Readable, unique task branch, e.g. debugops/dbg-001-login-api-returns-500 (always under debugops/)."""
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40].rstrip("-")
    return f"debugops/{task_id(run_id).lower()}" + (f"-{slug}" if slug else "")


def conninfo() -> str:
    return get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://")


def default_deps() -> Deps:
    from app.agents.claude_code import ClaudeCodeRunner
    from app.agents.llm import AnthropicLLM
    from app.github.client import get_github
    from app.sandbox.runner import Sandbox

    s = get_settings()
    return Deps(gh=get_github(), llm=lambda: AnthropicLLM(s),
                code_agent=lambda ws: ClaudeCodeRunner(ws.sb, ws.gateway_token, s),
                sandbox=lambda label: Sandbox(s, label=label))


async def start_run(db: AsyncSession, bug: Bug, user_id: int | None) -> WorkflowRun | None:
    """Queue a debugging run. None if this bug already has an active run. Commits."""
    repo = await db.get(Repository, bug.repository_id)
    # Snapshot of the task as it was when the run started (the bug may be edited later).
    run = WorkflowRun(bug_id=bug.id, repository_id=bug.repository_id, started_by=user_id, status="queued", nodes={},
                      task_title=bug.title, task_description=bug.description, repository_full_name=repo.full_name)
    db.add(run)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        return None
    run.branch_name = task_branch(run.id, bug.title)
    await queue.enqueue(db, "workflow.run", {"run_id": run.id}, max_attempts=1)
    log_event(db, "workflow", f"Debugging run #{run.id} queued for bug #{bug.id}: {bug.title}", run_id=run.id)
    await db.commit()
    return run


async def execute(run_id: int, resume: dict | None = None, deps: Deps | None = None) -> None:
    deps = deps or default_deps()
    async with SessionLocal() as db:
        created = (await db.get(WorkflowRun, run_id)).created_at
    # Unique per run even if ids are ever reused (e.g. after restoring an older backup).
    cfg = {"configurable": {"thread_id": f"run-{run_id}-{int(created.timestamp() * 1e6)}"}, "recursion_limit": 200}
    async with AsyncPostgresSaver.from_conn_string(conninfo()) as saver:
        await saver.setup()
        graph = build_graph(deps).compile(checkpointer=saver)
        snap = await graph.aget_state(cfg)
        async with SessionLocal() as db:
            run = await db.get(WorkflowRun, run_id)
            if run.status in TERMINAL:
                return
            if resume is not None:
                inp = Command(resume=resume)
            elif snap.values:
                inp = None  # worker restarted mid-run: continue from the last completed node
                log_event(db, "workflow", f"Run #{run_id} resuming from checkpoint after {snap.values.get('attempt', 0)} attempt(s)", run_id=run_id)
            else:
                inp = {"run_id": run_id, "attempt": 0, "feedback": []}
            run.status = "running"
            await db.commit()

        error, final_status = None, None
        started = time.monotonic()
        try:
            await graph.ainvoke(inp, cfg)
        except RunCancelled:
            final_status = "cancelled"
        except Exception as e:
            from app.workflow.pr import PRError
            log.exception("run %s failed", run_id)
            error = f"{type(e).__name__}: {e}"
            final_status = "needs_human" if isinstance(e, PRError) else "failed"

        snap = await graph.aget_state(cfg)
        async with SessionLocal() as db:
            run = await db.get(WorkflowRun, run_id)
            bug = await db.get(Bug, run.bug_id)
            values = snap.values or {}
            # Active execution only: time spent waiting for approval/merge is not counted.
            run.execution_seconds = (run.execution_seconds or 0) + int(time.monotonic() - started)
            if final_status is None:
                if snap.next:
                    final_status = "waiting_approval"
                elif values.get("pr"):
                    final_status = "awaiting_merge"
                elif (values.get("approval") or {}).get("decision") == "reject":
                    final_status = "rejected"
                    bug.status = "triaged"
                else:
                    final_status = {"needs_human": "needs_human", "failed": "failed"}.get(values.get("outcome"), "needs_human")
            if run.status != "cancelled" or final_status == "cancelled":
                run.status = final_status
            if error:
                run.error = error[:4000]
            if run.status in TERMINAL:
                run.finished_at = datetime.now(UTC)
                if bug.status == "investigating":
                    bug.status = "triaged"
            if run.status in TERMINAL or run.status == "waiting_approval":
                release_workspace(run_id)  # never hold a sandbox while waiting for a human
                await release_worktree(db, run_id)
                await revoke_gateway_tokens(db, run_id)
            log_event(db, "workflow", f"Run #{run_id} is now {run.status}" + (f": {error}" if error else ""),
                      level="error" if run.status == "failed" else "info", run_id=run_id)
            await db.commit()
