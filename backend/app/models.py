import enum
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Timestamps:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Role(str, enum.Enum):
    admin = "admin"
    developer = "developer"
    reviewer = "reviewer"


class User(Timestamps, Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True)  # stored lower-cased
    name: Mapped[str] = mapped_column(String(200), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[Role] = mapped_column(Enum(Role, name="user_role"))
    is_active: Mapped[bool] = mapped_column(default=True)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Session(Base):
    __tablename__ = "sessions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)  # sha256 hex of cookie token
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ip: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(String(500))


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    actor_type: Mapped[str] = mapped_column(String(20))  # user | system | agent
    action: Mapped[str] = mapped_column(String(100), index=True)
    target_type: Mapped[str | None] = mapped_column(String(50))
    target_id: Mapped[str | None] = mapped_column(String(100))
    data: Mapped[dict] = mapped_column(JSONB, default=dict)
    ip: Mapped[str | None] = mapped_column(INET)


class JobStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"


class Job(Timestamps, Base):
    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_claim", "priority", "run_at", postgresql_where=text("status = 'queued'")),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(String(100))
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[JobStatus] = mapped_column(Enum(JobStatus, name="job_status"), default=JobStatus.queued)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    locked_by: Mapped[str | None] = mapped_column(String(100))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)


class ActivityEvent(Base):
    __tablename__ = "activity_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    level: Mapped[str] = mapped_column(String(10), default="info")
    type: Mapped[str] = mapped_column(String(30))  # action | file_read | command | test | error | info
    source: Mapped[str] = mapped_column(String(100))  # e.g. worker id or agent name
    run_id: Mapped[int | None] = mapped_column(BigInteger, index=True)  # workflow run, if any (no FK: logs outlive runs)
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict] = mapped_column(JSONB, default=dict)


class Worker(Base):
    __tablename__ = "workers"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GitHubInstallation(Timestamps, Base):
    __tablename__ = "github_installations"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    installation_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    account_login: Mapped[str] = mapped_column(String(200))
    account_type: Mapped[str] = mapped_column(String(20))  # User | Organization
    repository_selection: Mapped[str] = mapped_column(String(20), default="selected")  # all | selected
    permissions: Mapped[dict] = mapped_column(JSONB, default=dict)
    events: Mapped[list] = mapped_column(JSONB, default=list)
    suspended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Repository(Timestamps, Base):
    __tablename__ = "repositories"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    installation_id: Mapped[int] = mapped_column(ForeignKey("github_installations.id", ondelete="CASCADE"), index=True)
    github_repo_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    full_name: Mapped[str] = mapped_column(String(300))
    default_branch: Mapped[str] = mapped_column(String(255), default="main")
    private: Mapped[bool] = mapped_column(default=True)
    # Present in the installation's repo list right now. False = removed from the app on GitHub.
    accessible: Mapped[bool] = mapped_column(default=True)
    # Admin opt-in: DebugOps only works on enabled + accessible repositories.
    enabled: Mapped[bool] = mapped_column(default=False)
    settings: Mapped[dict] = mapped_column(JSONB, default=dict)
    last_access_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_access_ok: Mapped[bool | None] = mapped_column()


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    delivery_id: Mapped[str] = mapped_column(String(100), unique=True)  # X-GitHub-Delivery (replay guard)
    event: Mapped[str] = mapped_column(String(50), index=True)
    action: Mapped[str | None] = mapped_column(String(50))
    installation_id: Mapped[int | None] = mapped_column(BigInteger)  # GitHub's id, may be unknown to us
    repository_id: Mapped[int | None] = mapped_column(ForeignKey("repositories.id", ondelete="SET NULL"))
    # processed | stored (kept for later phases) | ignored | error
    status: Mapped[str] = mapped_column(String(20))
    error: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONB)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class ScanRun(Timestamps, Base):
    __tablename__ = "scan_runs"
    __table_args__ = (
        # At most one queued/running scan per repository.
        Index("uq_scan_runs_active", "repository_id", unique=True,
              postgresql_where=text("status IN ('queued', 'running')")),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    repository_id: Mapped[int] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"), index=True)
    trigger: Mapped[str] = mapped_column(String(20))  # manual | schedule | push
    requested_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(String(20), default="queued")  # queued | running | succeeded | failed
    ref: Mapped[str | None] = mapped_column(String(255))
    commit_sha: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    steps: Mapped[dict] = mapped_column(JSONB, default=dict)  # step -> {status, duration_ms, detail}
    summary: Mapped[dict] = mapped_column(JSONB, default=dict)
    error: Mapped[str | None] = mapped_column(Text)


class Bug(Timestamps, Base):
    __tablename__ = "bugs"
    __table_args__ = (
        Index("uq_bugs_repo_fingerprint", "repository_id", "fingerprint", unique=True),
        Index("ix_bugs_title_trgm", "title", postgresql_using="gin", postgresql_ops={"title": "gin_trgm_ops"}),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    repository_id: Mapped[int] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"), index=True)
    # github_issue | auto_test | auto_semgrep | auto_gitleaks | auto_osv
    source: Mapped[str] = mapped_column(String(30), index=True)
    title: Mapped[str] = mapped_column(String(500))
    description: Mapped[str] = mapped_column(Text, default="")
    severity: Mapped[str] = mapped_column(String(10), index=True)  # info | low | medium | high | critical
    # new | triaged | investigating | fix_proposed | pr_open | resolved | wont_fix | duplicate | closed
    status: Mapped[str] = mapped_column(String(20), default="new", index=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    duplicate_of: Mapped[int | None] = mapped_column(ForeignKey("bugs.id", ondelete="SET NULL"))
    affected_files: Mapped[list] = mapped_column(JSONB, default=list)  # [{path, line_start, line_end}]
    data: Mapped[dict] = mapped_column(JSONB, default=dict)  # rule ids, CWE, advisories, labels...
    github_issue_number: Mapped[int | None] = mapped_column(Integer)
    github_issue_url: Mapped[str | None] = mapped_column(String(500))
    first_seen_scan_id: Mapped[int | None] = mapped_column(ForeignKey("scan_runs.id", ondelete="SET NULL"))
    last_seen_scan_id: Mapped[int | None] = mapped_column(ForeignKey("scan_runs.id", ondelete="SET NULL"))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    occurrences: Mapped[int] = mapped_column(Integer, default=1)


class Evidence(Base):
    __tablename__ = "evidence"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    bug_id: Mapped[int] = mapped_column(ForeignKey("bugs.id", ondelete="CASCADE"), index=True)
    scan_run_id: Mapped[int | None] = mapped_column(ForeignKey("scan_runs.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(String(20))  # scanner | test_output | issue | log | stack_trace
    content: Mapped[str] = mapped_column(Text)  # always redacted before insert
    file_path: Mapped[str | None] = mapped_column(String(1000))
    line_start: Mapped[int | None] = mapped_column(Integer)
    line_end: Mapped[int | None] = mapped_column(Integer)
    commit_sha: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TestRun(Base):
    __tablename__ = "test_runs"
    __test__ = False  # not a pytest class
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    scan_run_id: Mapped[int | None] = mapped_column(ForeignKey("scan_runs.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("workflow_runs.id", ondelete="CASCADE"), index=True)
    attempt_no: Mapped[int | None] = mapped_column(Integer)
    commit_sha: Mapped[str | None] = mapped_column(String(64))
    phase: Mapped[str] = mapped_column(String(20), default="scan")  # scan | baseline | base | fix | full
    command: Mapped[str] = mapped_column(Text)
    exit_code: Mapped[int | None] = mapped_column(Integer)
    timed_out: Mapped[bool] = mapped_column(default=False)
    result_format: Mapped[str] = mapped_column(String(20))  # junit | jest | exit_code
    passed: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[int] = mapped_column(Integer, default=0)
    skipped: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    log_tail: Mapped[str] = mapped_column(Text, default="")  # redacted, truncated
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TestResult(Base):
    __tablename__ = "test_results"
    __test__ = False
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    test_run_id: Mapped[int] = mapped_column(ForeignKey("test_runs.id", ondelete="CASCADE"), index=True)
    test_id: Mapped[str] = mapped_column(String(1000))
    status: Mapped[str] = mapped_column(String(10))
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str] = mapped_column(Text, default="")
    is_regression: Mapped[bool] = mapped_column(default=False)


class WorkflowRun(Timestamps, Base):
    __tablename__ = "workflow_runs"
    __table_args__ = (
        # One active debugging run per bug.
        Index("uq_workflow_runs_active_bug", "bug_id", unique=True,
              postgresql_where=text("status IN ('queued', 'running', 'waiting_approval')")),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    bug_id: Mapped[int] = mapped_column(ForeignKey("bugs.id", ondelete="CASCADE"), index=True)
    repository_id: Mapped[int] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"), index=True)
    started_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    # queued | running | waiting_approval | awaiting_merge | succeeded | needs_human | failed | rejected | cancelled | closed
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    current_node: Mapped[str | None] = mapped_column(String(40))
    nodes: Mapped[dict] = mapped_column(JSONB, default=dict)  # node -> {status, started_at, finished_at, detail}
    base_sha: Mapped[str | None] = mapped_column(String(64))
    branch_name: Mapped[str | None] = mapped_column(String(255))
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    validation_verdict: Mapped[str | None] = mapped_column(String(20))  # verified | failed | unverifiable
    task_type: Mapped[str | None] = mapped_column(String(20))  # incident | bug | feature (set by classification)
    # ---- audit record (snapshots at start; summaries filled as the run progresses) ----
    task_title: Mapped[str] = mapped_column(String(500), default="")  # bug as it was when the run started
    task_description: Mapped[str] = mapped_column(Text, default="")
    repository_full_name: Mapped[str] = mapped_column(String(300), default="")
    files_changed: Mapped[list] = mapped_column(JSONB, default=list)  # final attempt's changed paths
    test_summary: Mapped[dict] = mapped_column(JSONB, default=dict)  # platform test results with the fix
    execution_seconds: Mapped[int] = mapped_column(Integer, default=0)  # active time, excluding waits for humans
    pr_url: Mapped[str | None] = mapped_column(String(500))
    tokens_in: Mapped[int] = mapped_column(BigInteger, default=0)
    tokens_out: Mapped[int] = mapped_column(BigInteger, default=0)
    cost_usd: Mapped[float] = mapped_column(default=0.0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)


class AgentStep(Base):
    __tablename__ = "agent_steps"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("workflow_runs.id", ondelete="CASCADE"), index=True)
    node: Mapped[str] = mapped_column(String(40))
    agent: Mapped[str] = mapped_column(String(40))  # bug_hunter | investigation | code_analyst | fix | testing | security | review | platform
    status: Mapped[str] = mapped_column(String(20), default="running")  # waiting | running | completed | failed
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    output: Mapped[dict] = mapped_column(JSONB, default=dict)
    tokens_in: Mapped[int] = mapped_column(BigInteger, default=0)
    tokens_out: Mapped[int] = mapped_column(BigInteger, default=0)
    cost_usd: Mapped[float] = mapped_column(default=0.0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)


class FixAttempt(Base):
    __tablename__ = "fix_attempts"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("workflow_runs.id", ondelete="CASCADE"), index=True)
    attempt_no: Mapped[int] = mapped_column(Integer)
    diff: Mapped[str] = mapped_column(Text, default="")  # unified diff vs base (fix + tests)
    files: Mapped[dict] = mapped_column(JSONB, default=dict)  # path -> new content, or null if deleted
    fix_paths: Mapped[list] = mapped_column(JSONB, default=list)
    test_paths: Mapped[list] = mapped_column(JSONB, default=list)
    rationale: Mapped[str] = mapped_column(Text, default="")  # Fix agent's claim (never shown to the reviewer)
    validation: Mapped[dict] = mapped_column(JSONB, default=dict)  # platform-produced evidence
    security: Mapped[dict] = mapped_column(JSONB, default=dict)
    review: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending | rejected | accepted
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Approval(Base):
    __tablename__ = "approvals"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("workflow_runs.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # open_pr
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    decision: Mapped[str] = mapped_column(String(10))  # approve | reject
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PullRequest(Timestamps, Base):
    __tablename__ = "pull_requests"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("workflow_runs.id", ondelete="CASCADE"), unique=True)
    repository_id: Mapped[int] = mapped_column(ForeignKey("repositories.id", ondelete="CASCADE"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    url: Mapped[str] = mapped_column(String(500))
    head_branch: Mapped[str] = mapped_column(String(255))
    head_sha: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(20), default="open")  # open | closed | merged
    merged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ci_status: Mapped[str] = mapped_column(String(20), default="pending")  # pending | success | failure | none
    checks: Mapped[list] = mapped_column(JSONB, default=list)
    review_state: Mapped[str | None] = mapped_column(String(30))  # approved | changes_requested | commented
    reviews: Mapped[list] = mapped_column(JSONB, default=list)  # [{user, state, at}]
    # ---- after merge: CI on the merge commit, deployments, production health ----
    merge_commit_sha: Mapped[str | None] = mapped_column(String(64), index=True)
    post_merge_ci_status: Mapped[str | None] = mapped_column(String(20))
    post_merge_checks: Mapped[list] = mapped_column(JSONB, default=list)
    deployments: Mapped[list] = mapped_column(JSONB, default=list)  # [{id, environment, state, url, at}]
    health: Mapped[dict] = mapped_column(JSONB, default=dict)  # {status, url, checks, started_at, until, bug_id}


class Report(Timestamps, Base):
    __tablename__ = "reports"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("workflow_runs.id", ondelete="CASCADE"), unique=True)
    root_cause: Mapped[str] = mapped_column(Text, default="")
    evidence: Mapped[list] = mapped_column(JSONB, default=list)
    affected: Mapped[list] = mapped_column(JSONB, default=list)
    suggested_fix: Mapped[str] = mapped_column(Text, default="")
    test_summary: Mapped[dict] = mapped_column(JSONB, default=dict)
    security_summary: Mapped[dict] = mapped_column(JSONB, default=dict)
    review: Mapped[dict] = mapped_column(JSONB, default=dict)
    markdown: Mapped[str] = mapped_column(Text, default="")


class GatewayToken(Base):
    """Per-run credential a sandbox uses to reach the LLM gateway. Never the real Anthropic key."""
    __tablename__ = "gateway_tokens"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("workflow_runs.id", ondelete="CASCADE"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    request_count: Mapped[int] = mapped_column(Integer, default=0)
    tokens_in: Mapped[int] = mapped_column(BigInteger, default=0)
    tokens_out: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Integration(Timestamps, Base):
    """External integrations whose connectivity must be verified before workflows may use them."""
    __tablename__ = "integrations"
    name: Mapped[str] = mapped_column(String(50), primary_key=True)  # e.g. aws-devops-agent
    enabled: Mapped[bool] = mapped_column(default=False)  # admin opt-in; only allowed after a successful live check
    last_check: Mapped[dict] = mapped_column(JSONB, default=dict)  # {ok, tools, missing_tools, error, checked_at, endpoint}
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Investigation(Timestamps, Base):
    """An AWS DevOps Agent investigation (production incidents). Findings are untrusted text: shown, never executed."""
    __tablename__ = "investigations"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_id: Mapped[int | None] = mapped_column(ForeignKey("workflow_runs.id", ondelete="CASCADE"), index=True)
    bug_id: Mapped[int | None] = mapped_column(ForeignKey("bugs.id", ondelete="SET NULL"), index=True)
    title: Mapped[str] = mapped_column(Text)  # what was sent to the agent (incident + local context)
    agent_task_id: Mapped[str | None] = mapped_column(String(200))
    agent_execution_id: Mapped[str | None] = mapped_column(String(200))
    # pending | CREATED | IN_PROGRESS | COMPLETED | FAILED | TIMED_OUT | ERROR | SKIPPED
    status: Mapped[str] = mapped_column(String(20), default="pending")
    records: Mapped[list] = mapped_column(JSONB, default=list)  # journal records [{type, text, at}]
    summary: Mapped[str] = mapped_column(Text, default="")  # SUMMARY / FINDING records, condensed
    recommendations: Mapped[list] = mapped_column(JSONB, default=list)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
