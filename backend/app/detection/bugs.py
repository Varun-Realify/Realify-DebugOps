"""Bug records: dedupe findings into bugs, ingest GitHub issues, create GitHub issues for detected bugs."""

import logging
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import audit
from app.detection.parsers import SEVERITIES, Finding, fingerprint, redact
from app.events import log_event
from app.github.client import GitHubApp, GitHubError
from app.models import Bug, Evidence, GitHubInstallation, Repository, ScanRun
from app.schemas import RepoSettings

log = logging.getLogger("debugops.bugs")

SECURITY_SOURCES = {"auto_semgrep", "auto_gitleaks", "auto_osv"}
CLOSED_STATUSES = {"resolved", "closed", "wont_fix"}
MARKER = "<!-- debugops:fp={} -->"


def _merge_in_scan(findings: list[Finding]) -> list[tuple[Finding, list[dict]]]:
    """Same fingerprint twice in one scan (e.g. one secret in two places): one bug, all locations."""
    merged: dict[str, tuple[Finding, list[dict]]] = {}
    for f in findings:
        loc = {"path": f.file_path, "line_start": f.line_start, "line_end": f.line_end} if f.file_path else None
        if f.fingerprint in merged:
            if loc and loc not in merged[f.fingerprint][1]:
                merged[f.fingerprint][1].append(loc)
        else:
            merged[f.fingerprint] = (f, [loc] if loc else [])
    return list(merged.values())


async def record_findings(db: AsyncSession, repo: Repository, scan: ScanRun, findings: list[Finding]) -> dict:
    """Upsert findings as bugs keyed by (repository, fingerprint). Caller commits."""
    stats = {"new": 0, "seen_again": 0, "reopened": 0}
    items = _merge_in_scan(findings)
    if not items:
        return stats
    existing = {b.fingerprint: b for b in (await db.execute(select(Bug).where(
        Bug.repository_id == repo.id, Bug.fingerprint.in_([f.fingerprint for f, _ in items])))).scalars()}
    now = datetime.now(UTC)
    for f, locs in items:
        bug = existing.get(f.fingerprint)
        if bug is None:
            bug = Bug(repository_id=repo.id, source=f.source, title=f.title[:500], description=f.description,
                      severity=f.severity, status="new", fingerprint=f.fingerprint, affected_files=locs, data=f.data,
                      first_seen_scan_id=scan.id, last_seen_scan_id=scan.id, last_seen_at=now, occurrences=1)
            db.add(bug)
            await db.flush()
            stats["new"] += 1
            add_evidence = True
        else:
            stats["seen_again"] += 1
            if bug.status in ("resolved", "closed"):  # detected again after being closed: regression
                bug.status = "new"
                bug.data = {**bug.data, "reopened_by_scan": scan.id}
                stats["reopened"] += 1
            bug.last_seen_scan_id, bug.last_seen_at = scan.id, now
            bug.occurrences += 1
            bug.affected_files = locs or bug.affected_files
            latest = await db.scalar(select(Evidence.content).where(Evidence.bug_id == bug.id)
                                     .order_by(Evidence.id.desc()).limit(1))
            add_evidence = latest != f.evidence  # only keep evidence that changed
        if add_evidence:
            db.add(Evidence(bug_id=bug.id, scan_run_id=scan.id, kind=f.evidence_kind, content=f.evidence,
                            file_path=f.file_path, line_start=f.line_start, line_end=f.line_end, commit_sha=scan.commit_sha))
    return stats


# ---------------- GitHub issue ingestion ----------------

def _severity_from_labels(labels: list[str]) -> str:
    names = {lbl.lower().replace("severity:", "").replace("sev:", "").replace("priority:", "").strip() for lbl in labels}
    for sev in reversed(SEVERITIES):
        if sev in names:
            return sev
    return "critical" if {"p0", "blocker"} & names else "high" if "p1" in names else "medium"


def wants_issue(settings: RepoSettings, issue: dict, app_slug: str) -> bool:
    if "pull_request" in issue:
        return False
    if (issue.get("performed_via_github_app") or {}).get("slug") == app_slug and app_slug:
        return False  # we created it; the bug already exists
    if "<!-- debugops:fp=" in (issue.get("body") or ""):
        return False
    if not settings.ingest_labels:
        return True
    wanted = {lbl.lower() for lbl in settings.ingest_labels}
    return any(lbl["name"].lower() in wanted for lbl in issue.get("labels") or [])


async def ingest_issue(db: AsyncSession, repo: Repository, issue: dict, action: str | None = None) -> Bug | None:
    """Create/update the bug mirroring a GitHub issue. Caller commits."""
    fp = fingerprint("github_issue", str(issue["number"]))
    bug = (await db.execute(select(Bug).where(Bug.repository_id == repo.id, Bug.fingerprint == fp))).scalar_one_or_none()
    labels = [lbl["name"] for lbl in issue.get("labels") or []]
    body = redact((issue.get("body") or "")[:20000])
    title = issue.get("title", "")[:500]
    if bug is None:
        if issue.get("state") == "closed":
            return None  # don't import already-closed issues
        bug = Bug(repository_id=repo.id, source="github_issue", fingerprint=fp, title=title, description=body,
                  severity=_severity_from_labels(labels), status="new", affected_files=[],
                  github_issue_number=issue["number"], github_issue_url=issue.get("html_url"),
                  data={"labels": labels, "author": (issue.get("user") or {}).get("login")})
        db.add(bug)
        await db.flush()
        db.add(Evidence(bug_id=bug.id, kind="issue", content=f"{title}\n\n{body}"))
        log_event(db, "github", f"Bug report ingested from issue #{issue['number']} in {repo.full_name}: {title}")
        return bug

    changed = bug.title != title or bug.description != body
    bug.title, bug.description = title, body
    bug.data = {**bug.data, "labels": labels}
    if issue.get("state") == "closed" and bug.status not in CLOSED_STATUSES:
        bug.status = "closed"  # closed on GitHub; NOT the same as verified fixed
    elif action == "reopened" and bug.status in CLOSED_STATUSES:
        bug.status = "new"
    if changed:
        db.add(Evidence(bug_id=bug.id, kind="issue", content=f"{title}\n\n{body}"))
    return bug


# ---------------- GitHub issue creation ----------------

class IssueCreationRefused(Exception):
    pass


def _fence(text: str) -> str:
    return "~~~~\n" + text.replace("~~~~", "~ ~ ~ ~") + "\n~~~~"


def issue_body(bug: Bug, repo: Repository, evidence: Evidence | None, web_url: str) -> str:
    sha = evidence.commit_sha if evidence and evidence.commit_sha else repo.default_branch
    lines = [f"**Severity:** {bug.severity}  ", f"**Detected by:** Realify DebugOps ({bug.source.removeprefix('auto_')})  ", ""]
    if bug.description:
        lines += [bug.description, ""]
    if bug.affected_files:
        lines.append("**Affected files**")
        for loc in bug.affected_files[:20]:
            anchor = f"#L{loc['line_start']}" + (f"-L{loc['line_end']}" if loc.get("line_end") and loc["line_end"] != loc["line_start"] else "") if loc.get("line_start") else ""
            lines.append(f"- [{loc['path']}{':' + str(loc['line_start']) if loc.get('line_start') else ''}]({web_url}/{repo.full_name}/blob/{sha}/{loc['path']}{anchor})")
        lines.append("")
    if evidence:
        lines += ["**Evidence** (secrets redacted)", _fence(evidence.content[:6000]), ""]
    refs = (bug.data or {}).get("references") or []
    if refs:
        lines += ["**References**"] + [f"- {r}" for r in refs[:5]] + [""]
    lines += ["_This issue was opened automatically. AI-generated analysis must be reviewed by a human._", MARKER.format(bug.fingerprint)]
    return "\n".join(lines)


async def create_github_issue(db: AsyncSession, gh: GitHubApp, bug: Bug, *, actor_user_id: int | None, web_url: str) -> Bug:
    """Open a GitHub issue for a detected bug (or link the one that already exists). Caller commits."""
    repo = await db.get(Repository, bug.repository_id)
    inst = await db.get(GitHubInstallation, repo.installation_id)
    if bug.github_issue_number:
        return bug
    if bug.source == "github_issue":
        raise IssueCreationRefused("bug already originates from a GitHub issue")
    if not repo.private and bug.source in SECURITY_SOURCES:
        raise IssueCreationRefused("security findings are never filed as public issues on public repositories")
    if inst.permissions.get("issues") != "write":
        raise IssueCreationRefused("GitHub App lacks 'issues: write' permission (see docs/GITHUB_APP.md)")

    token = await gh.installation_token(inst.installation_id, repository_ids=[repo.github_repo_id],
                                        permissions={"issues": "write", "metadata": "read"})
    # Secondary dedupe: an issue carrying our marker may exist (e.g. DB restored from an older backup).
    try:
        found = await gh.get("/search/issues", token, params={"q": f'repo:{repo.full_name} "debugops:fp={bug.fingerprint}" in:body'})
        hit = next(iter(found.get("items") or []), None)
    except GitHubError:
        hit = None  # search is best-effort (indexing lag, rate limits); primary dedupe is the DB
    if hit is None:
        evidence = await db.scalar(select(Evidence).where(Evidence.bug_id == bug.id).order_by(Evidence.id.desc()).limit(1))
        payload = {"title": f"[DebugOps] {bug.title}"[:256], "body": issue_body(bug, repo, evidence, web_url), "labels": ["debugops"]}
        try:
            hit = await gh._send("POST", f"/repos/{repo.full_name}/issues", auth=f"token {token}", json=payload)
        except GitHubError as e:
            if e.status not in (403, 422):
                raise
            payload.pop("labels")  # label creation not permitted: file without it
            hit = await gh._send("POST", f"/repos/{repo.full_name}/issues", auth=f"token {token}", json=payload)
    bug.github_issue_number, bug.github_issue_url = hit["number"], hit["html_url"]
    audit(db, "bug.github_issue_created", actor_user_id=actor_user_id, actor_type="user" if actor_user_id else "system",
          target_type="bug", target_id=bug.id, issue=hit["number"], repository=repo.full_name)
    log_event(db, "github", f"Opened GitHub issue #{hit['number']} for bug #{bug.id} in {repo.full_name}")
    return bug


def meets_threshold(severity: str, minimum: str) -> bool:
    return SEVERITIES.index(severity) >= SEVERITIES.index(minimum)


async def similar_bugs(db: AsyncSession, bug: Bug, limit: int = 5) -> list[tuple[Bug, float]]:
    """Possible duplicates: open bugs in the same repository with similar titles (pg_trgm)."""
    sim = func.similarity(Bug.title, bug.title)
    rows = (await db.execute(select(Bug, sim).where(Bug.repository_id == bug.repository_id, Bug.id != bug.id,
                                                    Bug.status.not_in(["duplicate"]), sim > 0.35)
                             .order_by(sim.desc()).limit(limit))).all()
    return [(b, float(s)) for b, s in rows]
