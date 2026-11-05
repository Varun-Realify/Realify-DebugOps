import pytest
from sqlalchemy import select

from app.models import AuditLog, Role
from tests.conftest import PASSWORD, logged_in, login, make_user, new_client

ADMIN_ONLY = [("GET", "/api/v1/users"), ("POST", "/api/v1/users"), ("PATCH", "/api/v1/users/1"),
              ("DELETE", "/api/v1/users/1"), ("GET", "/api/v1/audit-logs")]


@pytest.mark.parametrize("method,path", ADMIN_ONLY)
async def test_admin_only_routes(method, path):
    async with new_client() as anon:
        assert (await anon.request(method, path)).status_code == 401
    for role in (Role.developer, Role.reviewer):
        async with logged_in(role) as c:
            assert (await c.request(method, path, json={})).status_code == 403, role


@pytest.mark.parametrize("role", list(Role))
async def test_dashboard_visible_to_all_roles(role):
    async with logged_in(role) as c:
        r = await c.get("/api/v1/dashboard/summary")
        assert r.status_code == 200
        body = r.json()
        assert set(body["jobs"]) == {"queued", "running", "succeeded", "failed"}
        assert body["users_by_role"][role.value] == 1


async def test_admin_user_lifecycle(db):
    async with logged_in(Role.admin) as admin:
        new = {"email": "Dev2@Example.com", "name": "Dev Two", "role": "developer", "password": PASSWORD}
        r = await admin.post("/api/v1/users", json=new)
        assert r.status_code == 201 and r.json()["email"] == "dev2@example.com"
        uid = r.json()["id"]
        assert (await admin.post("/api/v1/users", json=new)).status_code == 409
        assert (await admin.post("/api/v1/users", json={**new, "email": "x@example.com", "password": "short"})).status_code == 422
        assert (await admin.post("/api/v1/users", json={**new, "email": "y@example.com", "role": "root"})).status_code == 422

        async with new_client() as dev:
            await login(dev, "dev2@example.com")
            assert (await admin.patch(f"/api/v1/users/{uid}", json={"role": "reviewer"})).json()["role"] == "reviewer"
            assert (await dev.get("/api/v1/auth/me")).status_code == 401  # role change forces re-login

        assert (await admin.delete(f"/api/v1/users/{uid}")).status_code == 204
        async with new_client() as dev:
            assert (await login(dev, "dev2@example.com")).status_code == 401

    actions = (await db.execute(select(AuditLog.action))).scalars().all()
    assert {"user.created", "user.updated", "user.deactivated"} <= set(actions)


async def test_last_admin_cannot_be_removed():
    async with logged_in(Role.admin) as admin:
        me = (await admin.get("/api/v1/auth/me")).json()["id"]
        assert (await admin.patch(f"/api/v1/users/{me}", json={"role": "developer"})).status_code == 409
        assert (await admin.patch(f"/api/v1/users/{me}", json={"is_active": False})).status_code == 409
        assert (await admin.delete(f"/api/v1/users/{me}")).status_code == 409
        await make_user(Role.admin, email="admin2@example.com")
        assert (await admin.patch(f"/api/v1/users/{me}", json={"role": "developer"})).status_code == 200


async def test_admin_password_reset_unlocks_and_revokes():
    await make_user(Role.developer, email="locked@example.com")
    async with new_client() as c:
        for _ in range(5):
            await login(c, "locked@example.com", "wrong-password-123")
    async with logged_in(Role.admin) as admin:
        users = (await admin.get("/api/v1/users")).json()
        uid = next(u["id"] for u in users if u["email"] == "locked@example.com")
        assert (await admin.patch(f"/api/v1/users/{uid}", json={"password": "fresh-reset-passphrase"})).status_code == 200
    async with new_client() as c:
        assert (await login(c, "locked@example.com", "fresh-reset-passphrase")).status_code == 200


async def test_audit_log_listing():
    async with logged_in(Role.admin) as admin:
        logs = (await admin.get("/api/v1/audit-logs")).json()
        assert logs[0]["action"] == "auth.login"
