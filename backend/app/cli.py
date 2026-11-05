"""Admin CLI.

  python -m app.cli create-admin --email admin@example.com [--name "Ops Admin"]
  python -m app.cli github-check      # verify GitHub App credentials against the live API

Password is read from the DEBUGOPS_ADMIN_PASSWORD env var if set, otherwise prompted (never a CLI arg,
so it does not land in shell history or `ps` output).
"""

import argparse
import asyncio
import getpass
import os
import sys

from email_validator import EmailNotValidError, validate_email
from sqlalchemy import select

from app.audit import audit
from app.auth import MIN_PASSWORD_LEN, hash_password
from app.db import SessionLocal
from app.models import Role, User


async def create_admin(email: str, name: str, password: str) -> int:
    async with SessionLocal() as db:
        email = email.lower()
        if await db.scalar(select(User.id).where(User.email == email)):
            print(f"error: user {email} already exists", file=sys.stderr)
            return 1
        user = User(email=email, name=name, role=Role.admin, password_hash=hash_password(password))
        db.add(user)
        await db.flush()
        audit(db, "user.created", actor_type="system", target_type="user", target_id=user.id, email=email, role="admin", via="cli")
        await db.commit()
        print(f"created admin {email} (id={user.id})")
        return 0


async def github_check() -> int:
    from app.github.client import GitHubApp, GitHubError, GitHubNotConfigured
    from app.config import get_settings

    gh = GitHubApp(get_settings())
    try:
        me = await gh.app_get("/app")
        installs = await gh.paginate("/app/installations")
    except (GitHubError, GitHubNotConfigured, OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        await gh.aclose()
    print(f"ok: authenticated as app '{me['slug']}' (id={me['id']}, owner={me['owner']['login']})")
    print(f"permissions: {me.get('permissions')}")
    print(f"events: {me.get('events')}")
    print(f"installations: {[i['account']['login'] for i in installs] or 'none - install the app on your org'}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="cmd", required=True)
    ca = sub.add_parser("create-admin")
    ca.add_argument("--email", required=True)
    ca.add_argument("--name", default="")
    sub.add_parser("github-check")
    args = parser.parse_args()

    if args.cmd == "create-admin":
        try:  # same rules as accounts created through the API
            args.email = validate_email(args.email, check_deliverability=False).normalized
        except EmailNotValidError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        password = os.environ.get("DEBUGOPS_ADMIN_PASSWORD") or getpass.getpass("Password: ")
        if len(password) < MIN_PASSWORD_LEN:
            print(f"error: password must be at least {MIN_PASSWORD_LEN} characters", file=sys.stderr)
            return 1
        if "DEBUGOPS_ADMIN_PASSWORD" not in os.environ and getpass.getpass("Confirm: ") != password:
            print("error: passwords do not match", file=sys.stderr)
            return 1
        return asyncio.run(create_admin(args.email, args.name, password))
    if args.cmd == "github-check":
        return asyncio.run(github_check())
    return 2


if __name__ == "__main__":
    sys.exit(main())
