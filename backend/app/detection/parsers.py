"""Turn raw scanner / test-runner output into normalized findings and test results.

Pure functions, no I/O: unit-testable against captured tool output.
"""

import hashlib
import json
import re
import xml.etree.ElementTree as ET  # parses only our own sandbox output; defusedxml not needed for trusted-size files
from collections.abc import Callable
from dataclasses import dataclass, field

SEVERITIES = ("info", "low", "medium", "high", "critical")


@dataclass
class Finding:
    source: str  # auto_semgrep | auto_gitleaks | auto_osv | auto_test
    title: str
    description: str
    severity: str
    fingerprint: str
    evidence: str  # already redacted
    evidence_kind: str  # scanner | test_output
    file_path: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    data: dict = field(default_factory=dict)


@dataclass
class TestCase:
    test_id: str
    status: str  # passed | failed | error | skipped
    duration_ms: int = 0
    message: str = ""


def fingerprint(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


# ---------------- redaction ----------------

_SECRET_PATTERNS = [
    re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{40,}"),
    re.compile(r"(?:AKIA|ASIA)[A-Z0-9]{16}"),
    re.compile(r"xox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"sk-(?:ant-)?[A-Za-z0-9_-]{20,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key)(\s*[=:]\s*)(['\"]?)(?![^\s'\"]*\*\*\*\*)[^\s'\"]{6,}"),
]


def mask(secret: str) -> str:
    return (secret[:4] + "****") if len(secret) > 8 else "****"


def redact(text: str, known_secrets: list[str] = ()) -> str:
    """Remove secrets from any text before it is stored or displayed."""
    for s in known_secrets:
        if s:
            text = text.replace(s, mask(s))
    for p in _SECRET_PATTERNS:
        if p.groups >= 3:
            text = p.sub(lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}****", text)
        else:
            text = p.sub(lambda m: mask(m.group(0)), text)
    return text


# ---------------- scanners ----------------

LineReader = Callable[[str, int, int], str]  # (path, start, end) -> source lines; "" if unavailable


def _bump(sev: str, steps: int) -> str:
    return SEVERITIES[max(0, min(len(SEVERITIES) - 1, SEVERITIES.index(sev) + steps))]


def parse_semgrep(raw: str, read_lines: LineReader) -> list[Finding]:
    data = json.loads(raw or "{}")
    out = []
    for r in data.get("results", []):
        extra, meta = r.get("extra", {}), r.get("extra", {}).get("metadata", {})
        path, start, end = r["path"], r["start"]["line"], r["end"]["line"]
        impact = str(meta.get("impact", "")).lower()
        sev = impact if impact in ("low", "medium", "high") else {"ERROR": "high", "WARNING": "medium", "INFO": "low"}.get(extra.get("severity"), "medium")
        if str(meta.get("confidence", "")).upper() == "LOW":
            sev = _bump(sev, -1)
        snippet = read_lines(path, start, end)
        normalized = re.sub(r"\s+", " ", snippet).strip()
        rule = r["check_id"]
        out.append(Finding(
            source="auto_semgrep",
            title=f"{rule.rsplit('.', 1)[-1].replace('-', ' ')} in {path}",
            description=extra.get("message", "").strip(),
            severity=sev,
            # Line numbers excluded: code moving down a file is still the same finding.
            fingerprint=fingerprint("semgrep", rule, path, normalized or str(start)),
            evidence=redact(snippet or extra.get("lines", "")),
            evidence_kind="scanner",
            file_path=path, line_start=start, line_end=end,
            data={"rule_id": rule, "cwe": meta.get("cwe"), "owasp": meta.get("owasp"), "category": meta.get("category"),
                  "references": (meta.get("references") or [])[:5], "fix": extra.get("fix")},
        ))
    return out


def parse_gitleaks(raw: str) -> list[Finding]:
    """The raw report contains plaintext secrets: they are hashed for the fingerprint and masked here. Never stored."""
    out = []
    for g in json.loads(raw or "[]") or []:
        secret = g.get("Secret", "")
        path, rule = g.get("File", ""), g.get("RuleID", "secret")
        out.append(Finding(
            source="auto_gitleaks",
            title=f"Exposed secret ({rule}) in {path}",
            description=(g.get("Description") or "Hard-coded credential detected.") +
                        " Rotate this credential: removing it from the code does not revoke it.",
            severity="critical",
            fingerprint=fingerprint("gitleaks", rule, path, hashlib.sha256(secret.encode()).hexdigest()),
            evidence=redact(g.get("Match", ""), [secret]),
            evidence_kind="scanner",
            file_path=path, line_start=g.get("StartLine"), line_end=g.get("EndLine"),
            data={"rule_id": rule, "entropy": g.get("Entropy")},
        ))
    return out


def _cvss_to_severity(score: str | None) -> str:
    try:
        s = float(score or "")
    except ValueError:
        return "medium"
    return "critical" if s >= 9 else "high" if s >= 7 else "medium" if s >= 4 else "low"


def parse_osv(raw: str, root: str) -> list[Finding]:
    data = json.loads(raw or "{}")
    out = []
    root = root.rstrip("/") + "/"
    for res in data.get("results") or []:
        src = res.get("source", {}).get("path", "")
        src = src[len(root):] if src.startswith(root) else src
        for pkg in res.get("packages") or []:
            p = pkg["package"]
            vulns = {v["id"]: v for v in pkg.get("vulnerabilities") or []}
            for grp in pkg.get("groups") or []:
                ids = sorted(grp.get("ids") or [])
                if not ids:
                    continue
                v = next((vulns[i] for i in ids if i in vulns), {})
                cves = [a for a in grp.get("aliases") or [] if a.startswith("CVE-")]
                label = cves[0] if cves else ids[0]
                out.append(Finding(
                    source="auto_osv",
                    title=f"Vulnerable dependency {p['name']} {p.get('version', '')} ({label})",
                    description=(v.get("summary") or v.get("details") or "Known vulnerability.").strip()[:2000],
                    severity=_cvss_to_severity(grp.get("max_severity")),
                    fingerprint=fingerprint("osv", p.get("ecosystem", ""), p["name"], ids[0], src),
                    evidence=f"{p.get('ecosystem')} {p['name']}=={p.get('version')} in {src}; advisories: {', '.join(grp.get('aliases') or ids)}",
                    evidence_kind="scanner",
                    file_path=src or None,
                    data={"ids": ids, "aliases": grp.get("aliases"), "package": p, "cvss": grp.get("max_severity"),
                          "references": [r.get("url") for r in (v.get("references") or [])[:5]]},
                ))
    return out


# ---------------- tests ----------------

def parse_junit(raw: str) -> list[TestCase]:
    root = ET.fromstring(raw)
    cases = []
    for tc in root.iter("testcase"):
        cls, name = tc.get("classname", ""), tc.get("name", "")
        status, message = "passed", ""
        for tag in ("failure", "error", "skipped"):
            el = tc.find(tag)
            if el is not None:
                status = {"failure": "failed", "error": "error", "skipped": "skipped"}[tag]
                message = ((el.get("message") or "") + "\n" + (el.text or "")).strip()
                break
        cases.append(TestCase(test_id=f"{cls}::{name}" if cls else name, status=status,
                              duration_ms=int(float(tc.get("time") or 0) * 1000), message=message[:10000]))
    return cases


def parse_jest(raw: str) -> list[TestCase]:
    data = json.loads(raw)
    cases = []
    for suite in data.get("testResults", []):
        file = suite.get("name", "").split("/workspace/src/", 1)[-1]
        for a in suite.get("assertionResults", []):
            status = {"passed": "passed", "failed": "failed", "pending": "skipped", "todo": "skipped",
                      "skipped": "skipped"}.get(a.get("status"), "error")
            cases.append(TestCase(test_id=f"{file}::{a.get('fullName') or a.get('title')}", status=status,
                                  duration_ms=int(a.get("duration") or 0),
                                  message="\n".join(a.get("failureMessages") or [])[:10000]))
    return cases


_VOLATILE = re.compile(r"0x[0-9a-fA-F]+|\b\d+(\.\d+)?\b|'[^']*'|\"[^\"]*\"")


def error_signature(message: str) -> str:
    """First meaningful line with volatile values (numbers, addresses, quoted values) normalized."""
    line = next((ln.strip() for ln in message.splitlines() if ln.strip()), "")
    return _VOLATILE.sub("_", line)[:300]


def test_failures_to_findings(cases: list[TestCase]) -> list[Finding]:
    out = []
    for c in cases:
        if c.status not in ("failed", "error"):
            continue
        sig = error_signature(c.message)
        out.append(Finding(
            source="auto_test",
            title=f"Failing test: {c.test_id}"[:300],
            description=f"Test {'errored' if c.status == 'error' else 'failed'} on the default branch.",
            severity="high" if c.status == "error" else "medium",
            fingerprint=fingerprint("test", c.test_id, sig),
            evidence=redact(c.message[:8000]),
            evidence_kind="test_output",
            data={"test_id": c.test_id, "status": c.status, "signature": sig},
        ))
    return out
