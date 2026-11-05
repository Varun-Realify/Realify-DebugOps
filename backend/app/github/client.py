"""GitHub App client: app JWT, scoped installation tokens, REST calls.

Credentials live only in the api/worker processes. Installation tokens are always requested with
the narrowest repository list and permission set the caller needs, and cached until near expiry.
"""

import asyncio
import logging
import re
import time
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

import httpx
import jwt

from app.config import Settings, get_settings

log = logging.getLogger("debugops.github")

API_VERSION = "2022-11-28"
TOKEN_REFRESH_MARGIN = 300  # seconds before expiry we stop reusing a cached token
MAX_PAGES = 50


class GitHubError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"GitHub API {status}: {message}")
        self.status = status
        self.message = message


class GitHubNotConfigured(Exception):
    pass


def load_private_key(s: Settings) -> str:
    if s.github_app_private_key_file:
        return Path(s.github_app_private_key_file).read_text()
    return s.github_app_private_key.replace("\\n", "\n")


class GitHubApp:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.s = settings
        self._http = httpx.AsyncClient(
            base_url=settings.github_api_url.rstrip("/"),
            timeout=httpx.Timeout(20.0),
            transport=transport,
            headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": API_VERSION,
                     "User-Agent": "realify-debugops"},
        )
        self._tokens: dict[tuple, tuple[str, float]] = {}
        self._lock = asyncio.Lock()

    # ---------- auth ----------
    def app_jwt(self) -> str:
        if not self.s.github_configured:
            raise GitHubNotConfigured("GitHub App is not configured")
        now = int(time.time())
        try:
            # iat backdated 60s for clock skew; GitHub caps exp at 10 minutes.
            return jwt.encode({"iat": now - 60, "exp": now + 540, "iss": self.s.github_app_id},
                              load_private_key(self.s), algorithm="RS256")
        except (OSError, ValueError, jwt.PyJWTError) as e:
            raise GitHubNotConfigured(f"cannot load GitHub App private key: {type(e).__name__}") from e

    async def installation_token(self, installation_id: int, *, repository_ids: list[int] | None = None,
                                 permissions: dict[str, str] | None = None) -> str:
        """Token limited to `repository_ids` and `permissions`. Omit both only for listing an installation's repos."""
        key = (installation_id, tuple(sorted(repository_ids or [])), tuple(sorted((permissions or {}).items())))
        async with self._lock:
            cached = self._tokens.get(key)
            if cached and cached[1] - time.time() > TOKEN_REFRESH_MARGIN:
                return cached[0]
            body: dict = {}
            if repository_ids:
                body["repository_ids"] = repository_ids
            if permissions:
                body["permissions"] = permissions
            data = await self._send("POST", f"/app/installations/{installation_id}/access_tokens",
                                    auth=f"Bearer {self.app_jwt()}", json=body)
            expires = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00")).timestamp()
            self._tokens[key] = (data["token"], expires)
            return data["token"]

    # ---------- http ----------
    async def _send(self, method: str, path: str, *, auth: str, json: dict | None = None,
                    params: dict | None = None) -> dict | list:
        r = await self._raw(method, path, auth=auth, json=json, params=params)
        return r.json() if r.content else {}

    async def _raw(self, method, path, *, auth, json=None, params=None) -> httpx.Response:
        try:
            r = await self._http.request(method, path, json=json, params=params, headers={"Authorization": auth})
        except httpx.HTTPError as e:
            raise GitHubError(0, f"network error: {e}") from e
        if r.status_code >= 400:
            if r.status_code in (403, 429) and r.headers.get("x-ratelimit-remaining") == "0":
                reset = r.headers.get("x-ratelimit-reset", "")
                raise GitHubError(r.status_code, f"rate limited until {reset}")
            try:
                msg = r.json().get("message", r.text[:200])
            except ValueError:
                msg = r.text[:200]
            raise GitHubError(r.status_code, msg)
        return r

    async def app_get(self, path: str, params: dict | None = None):
        return await self._send("GET", path, auth=f"Bearer {self.app_jwt()}", params=params)

    async def get(self, path: str, token: str, params: dict | None = None):
        return await self._send("GET", path, auth=f"token {token}", params=params)

    async def paginate(self, path: str, *, token: str | None = None, key: str | None = None,
                       params: dict | None = None) -> list:
        """Follow Link: rel=next. `key` picks the list out of wrapped responses like {"repositories": [...]}."""
        items: list = []
        url, p = path, {"per_page": 100, **(params or {})}
        for _ in range(MAX_PAGES):
            auth = f"token {token}" if token else f"Bearer {self.app_jwt()}"
            r = await self._raw("GET", url, auth=auth, params=p)
            data = r.json()
            items.extend(data[key] if key else data)
            nxt = r.links.get("next", {}).get("url")
            if not nxt:
                return items
            if not nxt.startswith(self.s.github_api_url.rstrip("/") + "/"):
                raise GitHubError(0, "refusing to follow pagination link to another host")  # never leak a token
            url, p = nxt, None  # next URL already carries the query
        log.warning("pagination cap hit for %s", path)
        return items

    async def aclose(self):
        await self._http.aclose()


@lru_cache
def get_github() -> GitHubApp:
    return GitHubApp(get_settings())


# ---------- input validation for repository paths/refs ----------

_REF_RE = re.compile(r"^[A-Za-z0-9._/\-]{1,255}$")


def safe_repo_path(path: str) -> str:
    """Normalize a user-supplied repository path; reject anything that could escape or alter the URL."""
    path = (path or "").strip()
    if not path:
        return ""
    if len(path) > 1000 or any(c in path for c in ("\\", "\0", "?", "#")) or any(ord(c) < 32 for c in path):
        raise ValueError("invalid path")
    if path.startswith("/"):
        raise ValueError("path must be relative")
    parts = path.rstrip("/").split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise ValueError("path must not contain empty, '.' or '..' segments")
    return "/".join(parts)


def safe_ref(ref: str | None) -> str | None:
    if ref is None or ref == "":
        return None
    if not _REF_RE.match(ref) or ".." in ref or ref.startswith(("/", "-")) or ref.endswith(("/", ".lock")):
        raise ValueError("invalid ref")
    return ref


def contents_url(full_name: str, path: str) -> str:
    owner, repo = full_name.split("/", 1)
    encoded = "/".join(quote(seg, safe="") for seg in path.split("/")) if path else ""
    return f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/contents/{encoded}"
