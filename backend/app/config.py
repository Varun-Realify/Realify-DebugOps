from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = Field(description="postgresql+asyncpg://user:pass@host:5432/db")
    # Required by the api service (validated at its startup); other services (llm-gateway) do not receive it.
    secret_key: str = Field(default="", description="Used to derive CSRF tokens. Generate with `openssl rand -hex 32`.")

    cookie_secure: bool = True
    session_idle_minutes: int = 120
    session_absolute_hours: int = 12
    login_max_failures: int = 5
    login_lockout_minutes: int = 15
    api_docs_enabled: bool = False  # interactive /api/docs; static reference is docs/API.md
    login_rate_per_minute: int = 20  # per client IP, on top of per-account lockout
    allowed_origins: list[str] = []  # extra Origins allowed on websocket/mutations; same-origin always allowed
    log_level: str = "INFO"
    log_format: str = "json"  # json (centralized logging) | text
    sentry_dsn: str = ""  # optional error tracking (Sentry / GlitchTip)
    environment_name: str = "production"

    # GitHub App (see docs/GITHUB_APP.md). Unset = GitHub features disabled, app still runs.
    github_app_id: str = ""
    github_app_slug: str = ""  # for the "Install app" link
    github_app_private_key: str = ""  # PEM; literal "\n" sequences are accepted
    github_app_private_key_file: str = ""  # preferred: path to a read-only mounted PEM
    github_webhook_secret: str = ""
    github_api_url: str = "https://api.github.com"  # GitHub Enterprise Server: https://HOST/api/v3
    github_web_url: str = "https://github.com"

    # Sandbox (one ephemeral container per scan). Docker is reached via DOCKER_HOST (a socket proxy in compose).
    sandbox_image: str = "debugops-sandbox:latest"
    sandbox_network: str = "debugops-sandbox"  # internal-only network; "none" = no network at all
    sandbox_proxy_url: str = "http://egress-proxy:3128"  # allowlisting egress proxy; "" = none
    sandbox_memory: str = "2g"
    sandbox_cpus: float = 2.0
    sandbox_pids: int = 512
    sandbox_tmpfs_size: str = "1g"
    sandbox_scan_timeout_seconds: int = 2700  # hard cap for the whole container
    repo_cache_dir: str = "/var/lib/debugops/repos"  # persistent clones + per-task worktrees (worker volume)
    git_remote_base: str = ""  # override clone base URL (default: github_web_url); used by tests
    max_repo_bytes: int = 300_000_000  # uncompressed source size limit
    max_repo_files: int = 100_000

    # AI agents. The Anthropic key lives ONLY in worker + llm-gateway; sandboxes get a per-run gateway token.
    anthropic_api_key: str = ""
    anthropic_base_url: str = "https://api.anthropic.com"
    agent_model: str = "claude-opus-5-5"
    agent_effort: str = "high"  # low | medium | high | xhigh | max
    llm_gateway_url: str = "http://llm-gateway:8090"  # as seen from inside sandboxes
    llm_gateway_allowed_models: list[str] = ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]
    run_max_llm_requests: int = 600  # per workflow run, enforced by the gateway
    run_max_llm_tokens: int = 8_000_000  # input+output per run, enforced by the gateway
    claude_code_max_turns: int = 60
    agent_step_timeout_seconds: int = 1800
    root_cause_min_confidence: float = 0.6
    max_fix_attempts: int = 3

    # AWS DevOps Agent (production incident investigations) over its official remote MCP server.
    # Bearer token (single AgentSpace) for development; SigV4/IAM for production (see docs/AWS_DEVOPS_AGENT.md).
    devops_agent_token: str = ""  # secret: env or AWS Secrets Manager; never logged, never given to sandboxes
    devops_agent_region: str = "ap-south-1"
    devops_agent_url: str = ""  # default https://connect.aidevops.<region>.api.aws/mcp
    devops_agent_auth: str = "bearer"  # bearer | sigv4 (sigv4: planned, see docs)
    devops_agent_timeout_seconds: int = 120  # per MCP request (official plugin uses 120s)
    devops_agent_poll_seconds: int = 30  # never faster than 30s (AWS throttling guidance)
    devops_agent_max_minutes: int = 20  # investigations take 5-8 min; give up (with partial findings) after this

    # CloudWatch alarms via SNS: only these topic ARNs are accepted (empty = SNS endpoint disabled)
    sns_topic_arns: list[str] = []

    @property
    def devops_agent_endpoint(self) -> str:
        return self.devops_agent_url or f"https://connect.aidevops.{self.devops_agent_region}.api.aws/mcp"

    @property
    def devops_agent_configured(self) -> bool:
        return self.devops_agent_auth == "bearer" and bool(self.devops_agent_token)

    @property
    def agents_configured(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def github_configured(self) -> bool:
        return bool(self.github_app_id and (self.github_app_private_key or self.github_app_private_key_file)
                    and self.github_webhook_secret)


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
