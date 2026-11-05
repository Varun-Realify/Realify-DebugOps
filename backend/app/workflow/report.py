"""Final investigation report: what the platform knows, with evidence, and how sure it is."""

import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Bug, FixAttempt, Report, Repository, WorkflowRun

STATUS_LINE = {
    "ready": "✅ **Fix validated.** The regression test fails on the original code and passes with the fix; no previously passing test broke. Security and independent review passed. **A human must review and merge.**",
    "needs_human": "⚠️ **Needs human attention.** DebugOps could not produce a validated fix; see the reason below.",
    "failed": "❌ **No validated fix.** All automated attempts failed validation, security or review.",
}


def _hunk_ranges(diff: str) -> dict[str, list[tuple[int, int]]]:
    """path -> [(start, end)] of changed line ranges in the new file, from unified diff hunk headers."""
    out: dict[str, list[tuple[int, int]]] = {}
    path = None
    for ln in diff.splitlines():
        if ln.startswith("+++ "):
            path = None if ln[4:] == "/dev/null" else ln[6:] if ln.startswith("+++ b/") else ln[4:]
        m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", ln)
        if m and path:
            start, count = int(m.group(1)), int(m.group(2) or 1)
            out.setdefault(path, []).append((start, start + max(count, 1) - 1))
    return out


def _fence(text: str, lang: str = "") -> str:
    return f"~~~~{lang}\n" + text.replace("~~~~", "~ ~ ~ ~") + "\n~~~~"


async def build_report(db: AsyncSession, run: WorkflowRun, state: dict, outcome: str) -> Report:
    bug = await db.get(Bug, run.bug_id)
    repo = await db.get(Repository, run.repository_id)
    fa = await db.get(FixAttempt, state["attempt_id"]) if state.get("attempt_id") else \
        (await db.execute(select(FixAttempt).where(FixAttempt.run_id == run.id).order_by(FixAttempt.id.desc()).limit(1))).scalar_one_or_none()
    rc = state.get("root_cause") or {}
    validation = (fa.validation if fa else None) or state.get("validation") or {}
    ranges = _hunk_ranges(fa.diff) if fa else {}
    affected = [{"path": p, "ranges": ranges.get(p, []), "kind": "test" if p in (fa.test_paths if fa else []) else "source",
                 "deleted": fa.files.get(p) is None} for p in (fa.files if fa else {})]
    if not affected:
        affected = [{"path": e["file"], "ranges": [(e["line"], e["line"])], "kind": "cited"} for e in rc.get("evidence", [])]

    rep = (await db.execute(select(Report).where(Report.run_id == run.id))).scalar_one_or_none() or Report(run_id=run.id)
    rep.root_cause = rc.get("claim", "")
    rep.evidence = rc.get("evidence", [])
    rep.affected = affected
    rep.suggested_fix = (fa.diff if fa else "")[:200000]
    rep.test_summary = validation
    rep.security_summary = (fa.security if fa else {}) or {}
    rep.review = (fa.review if fa else {}) or {}

    from app.workflow.runner import task_id
    md = [f"## DebugOps report {task_id(run.id)}: {bug.title}", "", STATUS_LINE.get(outcome, outcome), ""]
    if state.get("outcome_reason"):
        md += [f"**Reason:** {state['outcome_reason']}", ""]
    md += [f"Repository `{repo.full_name}` at `{(run.base_sha or '')[:12]}` · bug #{bug.id} ({bug.source}, {bug.severity}) · "
           f"{run.attempt} fix attempt(s)", ""]
    if rc:
        md += ["### Root cause",
               f"{rc.get('claim', '')}  ",
               f"Confidence {rc.get('confidence', 0):.2f}" + (" · reproduced" if rc.get("reproduced") else " · not reproduced by command"), ""]
        if rc.get("evidence"):
            md.append("**Evidence** (citations verified against the source)")
            md += [f"- `{e['file']}:{e['line']}` {e.get('explanation', '')}" for e in rc["evidence"][:15]]
            md.append("")
        if rc.get("reproduction"):
            md += ["**Reproduction**", _fence(rc["reproduction"][:3000]), ""]
    if affected:
        md.append("### Affected files")
        for a in affected[:30]:
            lines = ", ".join(f"L{x}" if x == y else f"L{x}-L{y}" for x, y in a.get("ranges", [])[:6])
            md.append(f"- `{a['path']}` ({a['kind']}{', deleted' if a.get('deleted') else ''}) {lines}")
        md.append("")
    if validation:
        md += ["### Test evidence (produced by DebugOps, not by an agent)", f"**Verdict:** {validation.get('verdict')} — {validation.get('reason', '')}"]
        ev = validation.get("evidence") or {}
        for label, key in (("Baseline (original code)", "baseline"), ("Original code + new tests", "base_with_new_tests"), ("With fix", "with_fix")):
            if key in ev:
                c = ev[key]
                md.append(f"- {label}: {c.get('passed', 0)} passed, {c.get('failed', 0)} failed, {c.get('error', 0)} errors, {c.get('skipped', 0)} skipped")
        for t, r in list((ev.get("regression_tests") or {}).items())[:10]:
            md.append(f"- Regression `{t}`: {r.get('before_fix')} → {r.get('after_fix')}")
        md.append("")
    sec = rep.security_summary
    if sec:
        md += ["### Security", f"Verdict: **{sec.get('verdict')}**; new scanner findings in changed files: {len(sec.get('introduced_findings', []))}"]
        md += [f"- {i.get('severity')}: {i.get('file')}:{i.get('line')} {i.get('description')}" for i in (sec.get("agent") or {}).get("issues", [])[:10]]
        md.append("")
    if rep.review:
        md += ["### Independent review", f"Verdict: **{rep.review.get('verdict')}** — {rep.review.get('summary', '')}"]
        md += [f"- {i.get('severity')}: {i.get('file')}:{i.get('line')} {i.get('description')}" for i in rep.review.get("issues", [])[:10]]
        md.append("")
    if fa and fa.diff:
        md += ["<details><summary>Proposed change</summary>", "", _fence(fa.diff[:50000], "diff"), "", "</details>", ""]
    md.append("_Generated by Realify DebugOps. AI-generated changes must be reviewed by a human before merging._")
    rep.markdown = "\n".join(md)
    db.add(rep)
    await db.flush()
    return rep
