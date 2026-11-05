"""One-click GitHub App setup using GitHub's App Manifest flow.

  cd backend && uv run python ../scripts/create_github_app.py --webhook-url https://debugops.example.com [--org realify]

Opens a local page that posts a pre-filled App manifest to GitHub. After you click "Create GitHub App", GitHub
redirects back here; the script exchanges the one-time code for the App's credentials and writes them to
secrets/github-app.pem (mode 600) and .env (GITHUB_APP_ID, GITHUB_APP_SLUG, GITHUB_WEBHOOK_SECRET). Nothing is
printed or logged. Then restart: docker compose up -d
"""

import argparse
import html
import json
import os
import secrets
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx

ROOT = Path(__file__).resolve().parent.parent
PORT = 8765
EVENTS = ["issues", "issue_comment", "pull_request", "pull_request_review", "push", "check_run", "check_suite",
          "deployment", "deployment_status", "repository"]
PERMISSIONS = {"metadata": "read", "contents": "write", "issues": "write", "pull_requests": "write",
               "checks": "read", "deployments": "read"}


def update_env(values: dict[str, str]) -> None:
    env = ROOT / ".env"
    lines = env.read_text().splitlines() if env.exists() else []
    seen = set()
    for i, ln in enumerate(lines):
        key = ln.split("=", 1)[0].strip()
        if key in values:
            lines[i] = f"{key}={values[key]}"
            seen.add(key)
    lines += [f"{k}={v}" for k, v in values.items() if k not in seen]
    env.write_text("\n".join(lines) + "\n")
    os.chmod(env, 0o600)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--webhook-url", help="public base URL of DebugOps (https); omit with --no-webhook")
    ap.add_argument("--no-webhook", action="store_true",
                    help="local testing without a public URL: webhook inactive; use Sync / Import issues / Refresh instead")
    ap.add_argument("--org", help="create the App in this organization (default: your user account)")
    ap.add_argument("--name", default="Realify DebugOps")
    a = ap.parse_args()
    if not a.webhook_url and not a.no_webhook:
        ap.error("--webhook-url is required unless --no-webhook is given")
    base = (a.webhook_url or "https://example.invalid").rstrip("/")
    state = secrets.token_urlsafe(24)
    manifest = {"name": a.name, "url": base, "public": False, "redirect_url": f"http://127.0.0.1:{PORT}/callback",
                "hook_attributes": {"url": f"{base}/api/v1/github/webhook", "active": not a.no_webhook},
                "default_permissions": PERMISSIONS, "default_events": EVENTS}
    target = (f"https://github.com/organizations/{a.org}/settings/apps/new" if a.org else "https://github.com/settings/apps/new")
    done = threading.Event()
    result: dict = {}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def page(self, body: str, code: int = 200):
            data = f"<!doctype html><meta charset=utf-8><title>DebugOps GitHub App</title><body style='font:16px system-ui;margin:3rem'>{body}".encode()
            self.send_response(code)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            url = urlsplit(self.path)
            if url.path == "/":
                self.page(f"<form id=f method=post action='{target}?state={state}'><input type=hidden name=manifest "
                          f"value='{html.escape(json.dumps(manifest), quote=True)}'><p>Redirecting to GitHub…</p>"
                          "<button>Continue to GitHub</button></form><script>document.getElementById('f').submit()</script>")
                return
            if url.path == "/callback":
                q = parse_qs(url.query)
                if q.get("state", [""])[0] != state or "code" not in q:
                    self.page("Invalid callback (state mismatch).", 400)
                    return
                r = httpx.post(f"https://api.github.com/app-manifests/{q['code'][0]}/conversions",
                               headers={"Accept": "application/vnd.github+json"}, timeout=30)
                if r.status_code != 201:
                    self.page(f"GitHub returned {r.status_code}; try again.", 502)
                    return
                d = r.json()
                pem = ROOT / "secrets" / "github-app.pem"
                pem.parent.mkdir(exist_ok=True)
                pem.write_text(d["pem"])
                os.chmod(pem, 0o600)
                update_env({"GITHUB_APP_ID": str(d["id"]), "GITHUB_APP_SLUG": d["slug"],
                            "GITHUB_WEBHOOK_SECRET": d.get("webhook_secret") or secrets.token_hex(32),
                            "GITHUB_APP_PRIVATE_KEY_FILE": "/run/secrets/github-app.pem"})
                result.update(slug=d["slug"], id=d["id"], url=d["html_url"])
                self.page(f"<h2>GitHub App “{html.escape(d['name'])}” created.</h2><p>Credentials saved to .env and secrets/.</p>"
                          f"<p>Next: <a href='{html.escape(d['html_url'])}/installations/new'>install it on your repositories</a>, "
                          "then run <code>docker compose up -d</code>.</p>")
                done.set()
                return
            self.page("Not found", 404)

    srv = HTTPServer(("127.0.0.1", PORT), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    start = f"http://127.0.0.1:{PORT}/"
    print(f"Open {start} in your browser (opening it now) and click 'Create GitHub App'.")
    webbrowser.open(start)
    if not done.wait(timeout=900):
        print("Timed out waiting for GitHub.", file=sys.stderr)
        return 1
    srv.shutdown()
    print(f"Created GitHub App '{result['slug']}' (id {result['id']}). Credentials written to .env and secrets/github-app.pem.")
    print(f"Install it: {result['url']}/installations/new   then: docker compose up -d")
    return 0


if __name__ == "__main__":
    sys.exit(main())
