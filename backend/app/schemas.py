from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, IPvAnyAddress

from app.models import JobStatus, Role


class LoginIn(BaseModel):
    email: str = Field(max_length=320)  # lookup only; format is validated when accounts are created
    password: str = Field(max_length=1024)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    email: str
    name: str
    role: Role
    is_active: bool
    last_login_at: datetime | None
    created_at: datetime


class UserCreate(BaseModel):
    email: EmailStr
    name: str = Field(default="", max_length=200)
    role: Role
    password: str = Field(max_length=1024)


class UserUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    role: Role | None = None
    is_active: bool | None = None
    password: str | None = Field(default=None, max_length=1024)  # admin reset


class PasswordChange(BaseModel):
    current_password: str = Field(max_length=1024)
    new_password: str = Field(max_length=1024)


class ActivityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    ts: datetime
    level: str
    type: str
    source: str
    message: str


class WorkerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    started_at: datetime
    last_seen_at: datetime
    online: bool


class DashboardSummary(BaseModel):
    tasks: dict = {}  # total, active, awaiting_review, succeeded, failed, needs_human, success_rate
    errors_24h: int = 0
    agent_status: dict[str, int]  # running | waiting | completed (24h) | failed (24h)
    active_runs: list[dict]
    open_bugs_by_severity: dict[str, int]
    open_bugs_by_source: dict[str, int]
    latest_tests: dict[str, int]  # totals across each repository's most recent completed scan
    recent_scans: list[dict]
    jobs: dict[JobStatus, int]
    workers: list[WorkerOut]
    recent_activity: list[ActivityOut]
    users_by_role: dict[Role, int]


class AuditOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    ts: datetime
    actor_user_id: int | None
    actor_type: str
    action: str
    target_type: str | None
    target_id: str | None
    data: dict
    ip: IPvAnyAddress | None = None


Severity = Literal["info", "low", "medium", "high", "critical"]


class RepoSettings(BaseModel):
    """Per-repository detection settings (stored in repositories.settings). Admin-editable only."""
    run_semgrep: bool = True
    run_gitleaks: bool = True
    run_osv: bool = True
    run_tests: bool = True
    # Shell commands run INSIDE the sandbox (never on the host). Empty = auto-detect Python/Node.
    install_command: str | None = Field(default=None, max_length=2000)
    test_command: str | None = Field(default=None, max_length=2000)
    allow_install_scripts: bool = False  # npm lifecycle scripts during install
    step_timeout_seconds: int = Field(default=600, ge=30, le=3600)
    scan_on_push: bool = False  # scan when the default branch receives a push
    scan_interval_hours: int = Field(default=0, ge=0, le=720)  # 0 = no schedule
    ingest_labels: list[str] = Field(default_factory=lambda: ["bug", "debugops"], max_length=20)  # [] = all issues
    auto_create_issues: bool = False
    auto_issue_min_severity: Severity = "high"
    # Debugging workflow
    # Optional extra gate: a Reviewer/Admin approves in DebugOps before the PR is opened. Off by default: the AI opens
    # the PR and developers review, approve and merge it in GitHub. Merging is always a human action either way.
    require_pr_approval: bool = False
    comment_on_issue: bool = True  # post the investigation report on the originating GitHub issue
    # Delivery tracking after merge (DebugOps never deploys; it observes GitHub Deployments from your CD)
    production_environment: str = Field(default="production", max_length=100)
    health_check_url: str | None = Field(default=None, max_length=500, pattern=r"^https?://[^\s]+$")
    health_check_minutes: int = Field(default=30, ge=1, le=1440)
    health_check_interval_seconds: int = Field(default=60, ge=15, le=600)
    health_failure_threshold: int = Field(default=3, ge=1, le=20)  # consecutive failures that count as a regression
    investigate_regressions: bool = False  # start an AI run automatically for a failed post-deploy health check

    @classmethod
    def of(cls, raw: dict | None) -> "RepoSettings":
        return cls.model_validate(raw or {})
