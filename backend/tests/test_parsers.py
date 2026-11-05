"""Parser tests use output shapes captured from real Semgrep 1.183 / Gitleaks 8.30 / OSV-Scanner 2.7 runs."""

import json

from app.detection import parsers as p

SEMGREP = json.dumps({"results": [{
    "check_id": "python.lang.security.audit.subprocess-shell-true.subprocess-shell-true",
    "path": "app.py", "start": {"line": 5, "col": 39}, "end": {"line": 5, "col": 43},
    "extra": {"message": "Found 'subprocess' function with 'shell=True'.", "severity": "ERROR",
              "lines": "requires login",
              "metadata": {"impact": "LOW", "confidence": "MEDIUM", "category": "security",
                           "cwe": ["CWE-78"], "references": ["https://x"]}}}], "errors": []})

FAKE_TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"  # built at runtime, matches the github-pat shape

GITLEAKS = json.dumps([{"RuleID": "github-pat", "Description": "GitHub PAT", "StartLine": 1, "EndLine": 1,
                        "Match": f'TOKEN = "{FAKE_TOKEN}"', "Secret": FAKE_TOKEN, "File": "config.py",
                        "Entropy": 4.6, "Fingerprint": "config.py:github-pat:1"}])

OSV = json.dumps({"results": [{"source": {"path": "/workspace/src/requirements.txt", "type": "lockfile"},
                               "packages": [{"package": {"name": "django", "version": "2.2.0", "ecosystem": "PyPI"},
                                             "vulnerabilities": [{"id": "PYSEC-2019-11", "summary": "Django DoS"}],
                                             "groups": [{"ids": ["PYSEC-2019-11", "GHSA-c4qh-4vgv-qc6g"],
                                                         "aliases": ["CVE-2019-14232", "GHSA-c4qh-4vgv-qc6g"],
                                                         "max_severity": "8.7"}]}]}]})

JUNIT = """<?xml version="1.0"?><testsuites><testsuite name="pytest" tests="3">
<testcase classname="tests.test_app" name="test_add" time="0.002"><failure message="assert -1 == 5">def test_add():
&gt;   assert add(2, 3) == 5
E   assert -1 == 5</failure></testcase>
<testcase classname="tests.test_app" name="test_ok" time="0.001"/>
<testcase classname="tests.test_app" name="test_skip" time="0"><skipped message="later"/></testcase>
</testsuite></testsuites>"""


def lines(path, start, end):
    return {"app.py": "    return subprocess.call(cmd, shell=True)"}.get(path, "")


def test_semgrep():
    [f] = p.parse_semgrep(SEMGREP, lines)
    assert f.severity == "low"  # metadata.impact wins over the ERROR level
    assert f.evidence == "    return subprocess.call(cmd, shell=True)"  # real code, not "requires login"
    assert (f.file_path, f.line_start) == ("app.py", 5) and f.data["cwe"] == ["CWE-78"]
    moved = json.loads(SEMGREP)
    moved["results"][0]["start"]["line"] = moved["results"][0]["end"]["line"] = 40
    [g] = p.parse_semgrep(json.dumps(moved), lines)
    assert g.fingerprint == f.fingerprint  # same code moved down the file = same finding


def test_gitleaks_never_keeps_plaintext_secret():
    [f] = p.parse_gitleaks(GITLEAKS)
    assert FAKE_TOKEN not in f.evidence and FAKE_TOKEN not in f.fingerprint and FAKE_TOKEN not in json.dumps(f.data)
    assert "ghp_****" in f.evidence and f.severity == "critical"
    other = json.loads(GITLEAKS)
    other[0]["Secret"] = other[0]["Secret"][:-1] + "Z"
    assert p.parse_gitleaks(json.dumps(other))[0].fingerprint != f.fingerprint  # different secret, different bug


def test_osv():
    [f] = p.parse_osv(OSV, "/workspace/src")
    assert f.file_path == "requirements.txt" and f.severity == "high"
    assert "CVE-2019-14232" in f.title and f.data["ids"] == ["GHSA-c4qh-4vgv-qc6g", "PYSEC-2019-11"]


def test_junit_and_failure_findings():
    cases = p.parse_junit(JUNIT)
    assert [(c.test_id, c.status) for c in cases] == [
        ("tests.test_app::test_add", "failed"), ("tests.test_app::test_ok", "passed"), ("tests.test_app::test_skip", "skipped")]
    [f] = p.test_failures_to_findings(cases)
    assert f.source == "auto_test" and "assert -1 == 5" in f.evidence
    # Same failure with different runtime values keeps one fingerprint.
    cases[0].message = "assert -7 == 12"
    assert p.test_failures_to_findings(cases)[0].fingerprint == f.fingerprint


def test_jest():
    raw = json.dumps({"testResults": [{"name": "/workspace/src/sum.test.js", "assertionResults": [
        {"fullName": "sum adds", "status": "failed", "duration": 3, "failureMessages": ["Expected 3, got 4"]},
        {"fullName": "sum ok", "status": "passed", "duration": 1}, {"fullName": "later", "status": "pending"}]}]})
    assert [(c.test_id, c.status) for c in p.parse_jest(raw)] == [
        ("sum.test.js::sum adds", "failed"), ("sum.test.js::sum ok", "passed"), ("sum.test.js::later", "skipped")]


def test_redaction_patterns():
    text = (f"token={FAKE_TOKEN} aws AKIA" + "ABCDEFGHIJKLMNOP" + " password: hunter2hunter "
            "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----")
    out = p.redact(text)
    assert FAKE_TOKEN not in out and "ABCDEFGHIJKLMNOP" not in out and "hunter2hunter" not in out and "MIIabc" not in out


def test_empty_outputs():
    assert p.parse_semgrep("", lines) == [] and p.parse_gitleaks("") == [] and p.parse_gitleaks("null") == []
    assert p.parse_osv("", "/x") == []
