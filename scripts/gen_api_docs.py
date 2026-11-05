"""Regenerate docs/API.md from the application's OpenAPI schema.  cd backend && uv run python ../scripts/gen_api_docs.py"""
import os
import sys
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://x:x@localhost/x")
os.environ.setdefault("SECRET_KEY", "x" * 32)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
from app.main import app  # noqa: E402

spec = app.openapi()
lines = ["# API Reference", "", "Base path `/api/v1`. Generated from the code by `scripts/gen_api_docs.py` — do not edit by hand.", "",
         "**Authentication:** session cookie from `POST /api/v1/auth/login`. Every state-changing request must send the "
         "`X-CSRF-Token` header with the value of the `debugops_csrf` cookie. Cross-origin state changes are rejected.", "",
         "**Roles:** endpoints marked _Admin_, _Developer+_ (admin, developer) or _Approver_ (admin, reviewer); everything "
         "else needs any signed-in user. Public: `POST /auth/login`, `POST /github/webhook` (HMAC-signed), `/api/health/*`.", "",
         "**Live updates:** `WS /api/v1/ws?after=<event id>&run_id=<optional>` (session cookie; same-origin only). Messages: "
         "`{kind: event|run|scan|ready|ping, ...}`.", ""]
ROLE_HINT = {"Admin": "_Admin_", "Dev": "_Developer+_", "Approver": "_Approver_"}
by_tag: dict[str, list[str]] = {}
for path, ops in sorted(spec["paths"].items()):
    for method, op in ops.items():
        tag = (op.get("tags") or ["other"])[0]
        summary = op.get("summary", "")
        doc = (op.get("description") or "").strip().split("\n")[0]
        by_tag.setdefault(tag, []).append(f"| `{method.upper()}` | `{path}` | {summary}{' — ' + doc if doc else ''} |")
for tag, rows in by_tag.items():
    lines += [f"## {tag}", "", "| Method | Path | Description |", "|---|---|---|", *rows, ""]
out = Path(__file__).resolve().parent.parent / "docs" / "API.md"
out.write_text("\n".join(lines))
print(f"wrote {out} ({sum(len(r) for r in by_tag.values())} endpoints)")
