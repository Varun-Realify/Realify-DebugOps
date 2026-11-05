import hashlib
import hmac
import json
import uuid

import httpx
import jwt
import pytest
from sqlalchemy import select

from app.config import get_settings
from app.github import webhooks
from app.github.client import GitHubApp, GitHubError, get_github, safe_ref, safe_repo_path
from app.main import app
from app.models import AuditLog, GitHubInstallation, Repository, Role, WebhookDelivery
from tests.conftest import logged_in, new_client
from tests.fake_github import API, PUBLIC_PEM, FakeGitHub, installation, repo

SECRET = "webhook-test-secret"


@pytest.fixture
def fake():
    f = FakeGitHub()
    gh = GitHubApp(get_settings(), transport=f.transport())
    app.dependency_overrides[get_github] = lambda: gh
    yield f
    app.dependency_overrides.pop(get_github, None)


def sign(body: bytes, secret=SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


async def deliver(client, event: str, payload: dict, delivery: str | None = None, signature: str | None = None):
    body = json.dumps(payload).encode()
    headers = {"X-GitHub-Event": event, "X-GitHub-Delivery": delivery or str(uuid.uuid4()),
               "X-Hub-Signature-256": signature if signature is not None else sign(body),
               "Content-Type": "application/json"}
    return await client.post("/api/v1/github/webhook", content=body, headers=headers)


# ---------------- unit ----------------

def test_signature_matches_github_documented_vector():
    # https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries#testing-the-webhook-payload-validation
    sig = "sha256=757107ea0eb2509fc211221cce984b8a37570b6d7586c22c46f4379c8b043e17"
    assert webhooks.verify_signature("It's a Secret to Everybody", b"Hello, World!", sig)
    assert not webhooks.verify_signature("It's a Secret to Everybody", b"Hello, World?", sig)
    assert not webhooks.verify_signature("It's a Secret to Everybody", b"Hello, World!", sig.replace("sha256=", "sha1="))
    assert not webhooks.verify_signature("", b"Hello, World!", sig)
    assert not webhooks.verify_signature("x", b"", None)


def test_app_jwt_claims():
    token = GitHubApp(get_settings()).app_jwt()
    claims = jwt.decode(token, PUBLIC_PEM, algorithms=["RS256"])
    assert claims["iss"] == "12345"
    assert claims["exp"] - claims["iat"] <= 600  # GitHub's 10-minute cap


@pytest.mark.parametrize("bad", ["../etc/passwd", "a/../../b", "/abs", "a//b", "a/./b", "a\\b", "a\0b", "a?x=1", "a#f", "a\nb"])
def test_safe_repo_path_rejects_traversal(bad):
    with pytest.raises(ValueError):
        safe_repo_path(bad)


def test_safe_repo_path_and_ref_accept_normal():
    assert safe_repo_path("") == ""
    assert safe_repo_path("src/app/main.py") == "src/app/main.py"
    assert safe_repo_path("docs/") == "docs"
    assert safe_ref("feature/x-1.2") == "feature/x-1.2"
    for bad in ["../main", "-rf", "a b", "a..b", "x.lock", "x/"]:
        with pytest.raises(ValueError):
            safe_ref(bad)


async def test_installation_token_is_scoped_and_cached():
    f = FakeGitHub()
    gh = GitHubApp(get_settings(), transport=f.transport())
    t1 = await gh.installation_token(100, repository_ids=[1], permissions={"contents": "read"})
    t2 = await gh.installation_token(100, repository_ids=[1], permissions={"contents": "read"})
    t3 = await gh.installation_token(100, repository_ids=[2], permissions={"contents": "read"})
    assert t1 == t2 != t3
    assert f.token_requests == [{"repository_ids": [1], "permissions": {"contents": "read"}},
                                {"repository_ids": [2], "permissions": {"contents": "read"}}]
    assert f.requests[0].headers["authorization"].startswith("Bearer ")  # app JWT, not a token


async def test_pagination_follows_next_and_refuses_foreign_host():
    calls = []

    def handler(req):
        calls.append(str(req.url))
        if "page=2" in str(req.url):
            return httpx.Response(200, json=[{"id": 2}])
        return httpx.Response(200, json=[{"id": 1}], headers={"Link": f'<{API}/app/installations?page=2>; rel="next"'})

    gh = GitHubApp(get_settings(), transport=httpx.MockTransport(handler))
    assert [i["id"] for i in await gh.paginate("/app/installations")] == [1, 2]

    evil = GitHubApp(get_settings(), transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json=[], headers={"Link": '<https://evil.example/steal>; rel="next"'})))
    with pytest.raises(GitHubError, match="another host"):
        await evil.paginate("/app/installations")


async def test_unreadable_key_reports_cleanly(fake, monkeypatch):
    monkeypatch.setattr(get_settings(), "github_app_private_key_file", "/nonexistent/key.pem")
    async with logged_in(Role.admin) as c:
        assert "cannot load" in (await c.get("/api/v1/github/status")).json()["problem"]
        assert (await c.post("/api/v1/github/sync")).status_code == 503


async def test_rate_limit_error_is_explicit():
    gh = GitHubApp(get_settings(), transport=httpx.MockTransport(
        lambda r: httpx.Response(403, json={"message": "API rate limit exceeded"},
                                 headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1700000000"})))
    with pytest.raises(GitHubError, match="rate limited"):
        await gh.app_get("/app")


# ---------------- webhooks ----------------

async def test_webhook_rejects_bad_or_missing_signature(db):
    async with new_client() as c:
        assert (await deliver(c, "ping", {"zen": "hi"}, signature="")).status_code == 401
        assert (await deliver(c, "ping", {"zen": "hi"}, signature=sign(b"other"))).status_code == 401
        assert (await deliver(c, "ping", {"zen": "hi"}, signature=sign(b'{"zen": "hi"}', "wrong"))).status_code == 401
    assert (await db.execute(select(WebhookDelivery))).first() is None  # nothing stored


async def test_installation_lifecycle_via_webhooks(db):
    inst = installation()
    async with new_client() as c:
        r = await deliver(c, "installation", {"action": "created", "installation": inst,
                                              "repositories": [repo(1, "realify/api"), repo(2, "realify/web")]})
        assert r.json() == {"status": "processed"}
        repos = (await db.execute(select(Repository).order_by(Repository.github_repo_id))).scalars().all()
        assert [r.full_name for r in repos] == ["realify/api", "realify/web"]
        assert all(r.accessible and not r.enabled for r in repos)  # admin must opt in

        repos[0].enabled = True
        await db.commit()
        r = await deliver(c, "installation_repositories", {"action": "removed", "installation": inst,
                                                           "repositories_added": [], "repositories_removed": [repo(1, "realify/api")]})
        assert r.json() == {"status": "processed"}
        await db.refresh(repos[0])
        assert not repos[0].accessible and not repos[0].enabled

        await deliver(c, "installation", {"action": "deleted", "installation": inst})
        i = (await db.execute(select(GitHubInstallation))).scalar_one()
        await db.refresh(i)
        assert i.deleted_at is not None
        await db.refresh(repos[1])
        assert not repos[1].accessible
    actions = set((await db.execute(select(AuditLog.action))).scalars().all())
    assert {"github.installation.created", "github.repositories.removed", "github.installation.deleted"} <= actions


async def test_duplicate_delivery_ignored_and_issue_events_stored(db):
    async with new_client() as c:
        await deliver(c, "installation", {"action": "created", "installation": installation(), "repositories": [repo(1, "realify/api")]})
        payload = {"action": "opened", "installation": {"id": 100}, "repository": repo(1, "realify/api"),
                   "issue": {"number": 5, "title": "Crash on save", "labels": [{"name": "bug"}], "state": "open"}}
        assert (await deliver(c, "issues", payload)).json() == {"status": "ignored"}  # repository not enabled yet
        r = (await db.execute(select(Repository))).scalar_one()
        r.enabled = True
        await db.commit()
        first = await deliver(c, "issues", payload, delivery="dup-1")
        second = await deliver(c, "issues", payload, delivery="dup-1")
        assert first.json() == {"status": "processed"} and second.json() == {"status": "duplicate"}
        assert (await deliver(c, "star", {"action": "created"})).json() == {"status": "ignored"}
    d = (await db.execute(select(WebhookDelivery).where(WebhookDelivery.delivery_id == "dup-1"))).scalar_one()
    rid = await db.scalar(select(Repository.id).where(Repository.github_repo_id == 1))
    assert d.repository_id == rid and d.payload["issue"]["number"] == 5


async def test_failed_delivery_is_recorded_and_retryable(db, monkeypatch):
    async def boom(db, payload, action):
        raise RuntimeError("handler exploded")

    monkeypatch.setitem(webhooks.HANDLERS, "installation", boom)
    async with new_client() as c:
        payload = {"action": "created", "installation": installation(), "repositories": []}
        assert (await deliver(c, "installation", payload, delivery="retry-1")).status_code == 500
        d = (await db.execute(select(WebhookDelivery).where(WebhookDelivery.delivery_id == "retry-1"))).scalar_one()
        assert d.status == "error" and "handler exploded" in d.error
        monkeypatch.undo()
        r = await deliver(c, "installation", payload, delivery="retry-1")  # GitHub "Redeliver" reuses the GUID
        assert r.json() == {"status": "processed"}


# ---------------- API ----------------

async def test_github_routes_rbac(fake):
    for role in (Role.developer, Role.reviewer):
        async with logged_in(role) as c:
            assert (await c.get("/api/v1/repositories")).status_code == 200
            assert (await c.get("/api/v1/github/status")).status_code == 200
            assert (await c.post("/api/v1/github/sync")).status_code == 403
            assert (await c.patch("/api/v1/repositories/1", json={"enabled": True})).status_code == 403
            assert (await c.post("/api/v1/repositories/1/test-access")).status_code == 403
    async with new_client() as anon:
        assert (await anon.get("/api/v1/repositories")).status_code == 401


async def test_full_sync_enable_and_test_access(fake, db):
    async with logged_in(Role.admin) as c:
        status = (await c.get("/api/v1/github/status")).json()
        assert status["configured"] and status["install_url"].endswith("/apps/debugops-test/installations/new")

        assert (await c.post("/api/v1/github/sync")).json() == {"installations": 1, "repositories": 2, "removed_installations": 0}
        repos = (await c.get("/api/v1/repositories")).json()
        api_repo = next(r for r in repos if r["full_name"] == "realify/api")
        assert not api_repo["enabled"]

        # Repo removed from the app on GitHub: next sync marks it inaccessible.
        fake.repos[100] = [repo(1, "realify/api")]
        await c.post("/api/v1/github/sync")
        web = next(r for r in (await c.get("/api/v1/repositories")).json() if r["full_name"] == "realify/web")
        assert not web["accessible"]
        assert (await c.patch(f"/api/v1/repositories/{web['id']}", json={"enabled": True})).status_code == 409

        r = await c.patch(f"/api/v1/repositories/{api_repo['id']}", json={"enabled": True})
        assert r.status_code == 200 and r.json()["enabled"]

        fake.token_requests.clear()
        result = (await c.post(f"/api/v1/repositories/{api_repo['id']}/test-access")).json()
        assert result["ok"], result
        assert [ch["ok"] for ch in result["checks"]] == [True] * 6
        # Token was limited to this single repository and read-only permissions.
        assert fake.token_requests == [{"repository_ids": [1], "permissions": {p: "read" for p in
                                        ("metadata", "contents", "issues", "pull_requests", "checks")}}]


async def test_test_access_skips_ungranted_permissions(fake, db):
    fake.installations = [installation(perms={"metadata": "read", "contents": "read"})]
    async with logged_in(Role.admin) as c:
        await c.post("/api/v1/github/sync")
        rid = (await c.get("/api/v1/repositories")).json()[0]["id"]
        fake.token_requests.clear()
        result = (await c.post(f"/api/v1/repositories/{rid}/test-access")).json()
        assert fake.token_requests[0]["permissions"] == {"metadata": "read", "contents": "read"}
        skipped = [ch["name"] for ch in result["checks"] if ch["ok"] is None]
        assert skipped == ["Read issues", "Read pull requests", "Read CI checks"]


async def test_contents_browsing(fake, db):
    async with logged_in(Role.admin) as admin:
        await admin.post("/api/v1/github/sync")
        rid = next(r["id"] for r in (await admin.get("/api/v1/repositories")).json() if r["full_name"] == "realify/api")
        assert (await admin.get(f"/api/v1/repositories/{rid}/contents")).status_code == 409  # not enabled yet
        await admin.patch(f"/api/v1/repositories/{rid}", json={"enabled": True})

    async with logged_in(Role.developer) as dev:
        root = (await dev.get(f"/api/v1/repositories/{rid}/contents")).json()
        assert root["type"] == "dir" and [e["name"] for e in root["entries"]] == ["src", "README.md"]  # dirs first
        f = (await dev.get(f"/api/v1/repositories/{rid}/contents", params={"path": "README.md"})).json()
        assert f["content"] == "# API\n" and not f["binary"]
        assert (await dev.get(f"/api/v1/repositories/{rid}/contents", params={"path": "../../other/repo"})).status_code == 400
        assert (await dev.get(f"/api/v1/repositories/{rid}/contents", params={"path": "x", "ref": "../x"})).status_code == 400
        assert (await dev.get(f"/api/v1/repositories/{rid}/contents", params={"path": "missing.txt"})).status_code == 404
