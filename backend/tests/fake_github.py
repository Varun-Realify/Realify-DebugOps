"""In-process fake of the GitHub REST endpoints DebugOps calls. Records every request for assertions."""

import base64
import json
from datetime import UTC, datetime, timedelta

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PRIVATE_PEM = _key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                 serialization.NoEncryption()).decode()
PUBLIC_PEM = _key.public_key().public_bytes(serialization.Encoding.PEM,
                                            serialization.PublicFormat.SubjectPublicKeyInfo).decode()
API = "https://api.github.test"

PERMS = {"metadata": "read", "contents": "read", "issues": "read", "pull_requests": "read", "checks": "read"}


def installation(iid=100, login="realify", perms=None):
    return {"id": iid, "account": {"login": login, "type": "Organization"}, "repository_selection": "selected",
            "permissions": perms or dict(PERMS), "events": ["issues", "push"], "suspended_at": None}


def repo(rid, name, private=True, branch="main"):
    return {"id": rid, "name": name.split("/")[1], "full_name": name, "private": private, "default_branch": branch}


class FakeGitHub:
    def __init__(self):
        self.installations = [installation()]
        self.repos = {100: [repo(1, "realify/api"), repo(2, "realify/web")]}
        self.files = {"realify/api": {"": [("README.md", "file"), ("src", "dir")],
                                      "README.md": "# API\n", "src": [("main.py", "file")]}}
        self.requests: list[httpx.Request] = []
        self.tarball: bytes = b""  # gzip tar served for /tarball/
        self.issues: list[dict] = []
        self.created_issues: list[dict] = []
        self.search_items: list[dict] = []
        self.git_writes: list[tuple[str, dict]] = []  # (path, body) for git data / pulls / comments
        self.pull_state = {"state": "open", "merged": False, "merged_at": None}
        self.ref_conflict: str | None = None  # None | "foreign" | "ours:<run id>": the PR branch already exists
        self.token_requests: list[dict] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        p = req.url.path
        if p == "/app/installations":
            return httpx.Response(200, json=self.installations)
        if p.startswith("/app/installations/") and p.endswith("/access_tokens"):
            body = json.loads(req.content or b"{}")
            self.token_requests.append(body)
            n = len(self.token_requests)
            exp = (datetime.now(UTC) + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
            return httpx.Response(201, json={"token": f"ghs_test{n}", "expires_at": exp})
        if p == "/installation/repositories":
            iid = int(self.installations[0]["id"])
            repos = self.repos.get(iid, [])
            return httpx.Response(200, json={"total_count": len(repos), "repositories": repos})
        if req.url.host == "codeload.github.test":
            assert "authorization" not in req.headers, "token leaked to redirect target"
            return httpx.Response(200, content=self.tarball)
        if p == "/search/issues":
            return httpx.Response(200, json={"total_count": len(self.search_items), "items": self.search_items})
        if p.startswith("/repos/") and p.endswith("/issues") and req.method == "POST":
            body = json.loads(req.content)
            self.created_issues.append(body)
            n = 100 + len(self.created_issues)
            return httpx.Response(201, json={"number": n, "html_url": f"https://github.test/{p.split('/')[2]}/{p.split('/')[3]}/issues/{n}"})
        if "/git/" in p or p.endswith("/pulls") or "/comments" in p or "/pulls/" in p:
            if self.ref_conflict and req.method == "POST" and p.endswith("/git/refs"):
                return httpx.Response(422, json={"message": "Reference already exists"})
            if "/git/ref/heads/" in p:
                return httpx.Response(200, json={"object": {"sha": "existingtip"}})
            if p.endswith("/git/commits/existingtip"):
                msg = (f"fix: x\n\nAutomated fix proposed by Realify DebugOps (run #{self.ref_conflict.split(':')[1]}).\n"
                       if self.ref_conflict.startswith("ours") else "WIP: a developer's own work")
                return httpx.Response(200, json={"sha": "existingtip", "message": msg})
            if req.method in ("POST", "PATCH"):
                body = json.loads(req.content or b"{}")
                self.git_writes.append((p, body))
                n = len(self.git_writes)
                if p.endswith("/pulls"):
                    return httpx.Response(201, json={"number": 77, "html_url": "https://github.test/realify/api/pull/77"})
                return httpx.Response(201, json={"sha": f"sha{n:037d}", "id": n})
            if "/git/commits/" in p:
                return httpx.Response(200, json={"sha": p.rsplit("/", 1)[-1], "tree": {"sha": "basetree"}})
            if "/git/trees/" in p:
                return httpx.Response(200, json={"tree": [{"path": "run_tests.py", "mode": "100755", "type": "blob"}], "truncated": False})
            if "/pulls/" in p:
                return httpx.Response(200, json={"number": 77, "head": {"sha": "headsha"}, **self.pull_state})
        if "/tarball/" in p:
            return httpx.Response(302, headers={"Location": "https://codeload.github.test/archive.tar.gz?token=presigned"})
        if p.startswith("/repos/"):
            parts = p.split("/")
            full = f"{parts[2]}/{parts[3]}"
            rest = "/".join(parts[4:])
            if not any(r["full_name"] == full for rs in self.repos.values() for r in rs):
                return httpx.Response(404, json={"message": "Not Found"})
            if rest == "":
                for rs in self.repos.values():
                    for r in rs:
                        if r["full_name"] == full:
                            return httpx.Response(200, json=r)
                return httpx.Response(404, json={"message": "Not Found"})
            if rest.startswith("contents"):
                path = rest[len("contents"):].lstrip("/")
                node = self.files.get(full, {}).get(path)
                if node is None:
                    return httpx.Response(404, json={"message": "Not Found"})
                if isinstance(node, list):
                    return httpx.Response(200, json=[{"name": n, "path": f"{path}/{n}".lstrip("/"), "type": t, "size": 10}
                                                     for n, t in node])
                return httpx.Response(200, json={"type": "file", "size": len(node), "sha": "abc",
                                                 "content": base64.b64encode(node.encode()).decode()})
            if rest.endswith("/check-runs"):
                return httpx.Response(200, json={"total_count": 3, "check_runs": []})
            if rest.startswith("commits/"):
                return httpx.Response(200, json={"sha": "0123456789abcdef", "commit": {"message": "Initial commit\n\nbody"}})
            if rest == "issues":
                return httpx.Response(200, json=self.issues or [{"number": 7}])
            if rest == "pulls":
                return httpx.Response(200, json=[])
        return httpx.Response(404, json={"message": f"fake: no route {req.method} {p}"})
