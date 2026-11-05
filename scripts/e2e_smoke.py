"""End-to-end smoke test against a running DebugOps deployment.

  cd backend && uv run python ../scripts/e2e_smoke.py --url https://debugops.example.com --email admin@example.com
  (password from DEBUGOPS_SMOKE_PASSWORD or prompted)

Checks health, login and lockout-safe failure, CSRF and cross-origin protection, RBAC, security headers, the live
WebSocket (auth, origin check, backlog/ready), and logout revocation. Read-only apart from one login/logout.
`--expect-event N`: also wait up to N seconds for a live activity event (trigger one, e.g. start a scan).
"""

import argparse
import asyncio
import getpass
import json
import os
import sys
from urllib.parse import urlsplit

import httpx
import websockets

results: list[tuple[bool, str]] = []


def check(ok: bool, what: str) -> None:
    results.append((ok, what))
    print(("PASS " if ok else "FAIL ") + what)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8088")
    ap.add_argument("--email", required=True)
    ap.add_argument("--expect-event", type=int, default=0)
    a = ap.parse_args()
    password = os.environ.get("DEBUGOPS_SMOKE_PASSWORD") or getpass.getpass("Password: ")
    base, host = a.url.rstrip("/"), urlsplit(a.url).netloc

    async with httpx.AsyncClient(base_url=base, timeout=20) as c:
        r = await c.get("/api/health/ready")
        check(r.status_code == 200 and r.json().get("status") == "ok", "api ready (database reachable)")
        check(r.json().get("workers_online", 0) >= 1, f"worker online ({r.json().get('workers_online')})")
        idx = await c.get("/")
        check("default-src 'self'" in idx.headers.get("content-security-policy", ""), "CSP header on the web app")
        check(idx.headers.get("x-frame-options") == "DENY", "clickjacking protection")
        check((await c.get("/api/v1/auth/me")).status_code == 401, "unauthenticated request rejected")
        check((await c.post("/api/v1/auth/login", json={"email": a.email, "password": "definitely-wrong-" * 2})).status_code == 401,
              "wrong password rejected")
        r = await c.post("/api/v1/auth/login", json={"email": a.email, "password": password})
        check(r.status_code == 200, "login")
        if r.status_code != 200:
            return 1
        role = r.json()["role"]
        csrf = c.cookies.get("debugops_csrf")
        set_cookie = ",".join(r.headers.get_list("set-cookie"))
        check("HttpOnly" in set_cookie and "SameSite=lax" in set_cookie, "session cookie is HttpOnly + SameSite")
        if base.startswith("https"):
            check("Secure" in set_cookie, "session cookie is Secure")
        check((await c.post("/api/v1/auth/logout")).status_code == 403, "state change without CSRF token rejected")
        check((await c.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf, "Origin": "https://evil.example"})).status_code == 403,
              "cross-origin state change rejected")
        check((await c.get("/api/v1/dashboard/summary")).status_code == 200, "dashboard summary")
        admin_status = (await c.get("/api/v1/users")).status_code
        check(admin_status == (200 if role == "admin" else 403), f"RBAC on admin route for role {role}")

        cookie = f"debugops_session={c.cookies.get('debugops_session')}"
        ws_url = base.replace("http", "ws", 1) + "/api/v1/ws"
        for headers, label in (({}, "without session"), ({"Cookie": cookie, "Origin": "https://evil.example"}, "from a foreign origin")):
            try:
                async with websockets.connect(ws_url, additional_headers=headers) as w:
                    await asyncio.wait_for(w.recv(), 3)
                check(False, f"WebSocket {label} rejected")
            except (websockets.exceptions.InvalidStatus, websockets.exceptions.ConnectionClosed):
                check(True, f"WebSocket {label} rejected")
        async with websockets.connect(ws_url + "?after=0", additional_headers={"Cookie": cookie, "Origin": f"{urlsplit(base).scheme}://{host}"}) as w:
            got_ready, got_event = False, False
            deadline = asyncio.get_running_loop().time() + 10 + a.expect_event
            while asyncio.get_running_loop().time() < deadline and not (got_ready and (got_event or not a.expect_event)):
                try:
                    msg = json.loads(await asyncio.wait_for(w.recv(), 2))
                except TimeoutError:
                    continue
                got_ready |= msg.get("kind") == "ready"
                got_event |= got_ready and msg.get("kind") == "event"
            check(got_ready, "WebSocket authenticated, backlog replayed, stream ready")
            if a.expect_event:
                check(got_event, "live activity event delivered over WebSocket")

        token = c.cookies.get("debugops_session")
        check((await c.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf})).status_code == 204, "logout")
        async with httpx.AsyncClient(base_url=base) as c2:
            c2.cookies.set("debugops_session", token)
            check((await c2.get("/api/v1/auth/me")).status_code == 401, "session revoked after logout")

    failed = [w for ok, w in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
