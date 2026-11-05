"""Cross-cutting security checks: every route is authenticated unless explicitly public, plus login throttling."""

import pytest

from app.api import auth as auth_api
from app.config import get_settings
from app.main import app
from tests.conftest import new_client

PUBLIC = {("POST", "/api/v1/auth/login"), ("POST", "/api/v1/github/webhook"),
          ("GET", "/api/health/live"), ("GET", "/api/health/ready")}


def all_routes():
    # Generated from the app itself, so new routes are covered automatically.
    for path, ops in app.openapi()["paths"].items():
        for method in ops:
            yield method.upper(), path


def test_public_route_list_is_exactly_what_we_expect():
    routes = set(all_routes())
    assert PUBLIC <= routes
    assert len(routes) > 40
    assert app.docs_url is None and app.openapi_url is None  # interactive docs off by default


@pytest.mark.parametrize("method,path", sorted(set(all_routes()) - PUBLIC))
async def test_every_other_route_requires_authentication(method, path):
    url = path.replace("{", "").replace("}", "")  # concrete-enough placeholder values
    url = "/".join("1" if seg.endswith("_id") else seg for seg in url.split("/"))
    async with new_client() as c:
        r = await c.request(method, url, json={})
        assert r.status_code == 401, f"{method} {path} -> {r.status_code}"


async def test_login_is_throttled_per_ip(monkeypatch):
    monkeypatch.setattr(get_settings(), "login_rate_per_minute", 3)
    auth_api._attempts.clear()
    async with new_client() as c:
        codes = [(await c.post("/api/v1/auth/login", json={"email": "x@example.com", "password": "nope-nope-nope"})).status_code
                 for _ in range(4)]
    auth_api._attempts.clear()
    assert codes == [401, 401, 401, 429]


async def test_security_headers():
    async with new_client() as c:
        r = await c.get("/api/health/live")
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "DENY"
    assert r.headers["cache-control"] == "no-store"
