"""Local operator CLI. Passwords are entered securely, never via command arguments."""

import argparse
import sys
from getpass import getpass
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from auth import hash_password
from database import SessionLocal
from models import User


def prompt_password() -> str:
    if not sys.stdin.isatty():
        raise ValueError("Password entry requires an interactive terminal.")
    password = getpass("Password (12-128 characters): ")
    if password != getpass("Confirm password: "):
        raise ValueError("Passwords do not match.")
    return hash_password(password)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        choices=["create", "reset-password", "set-role", "disable", "enable", "revoke"],
    )
    parser.add_argument("--username")
    parser.add_argument("--role", choices=["admin", "provider"])
    args = parser.parse_args()
    username = (args.username or input("Username: ")).strip().lower()
    # Validate username with the API contract; this object is never logged.
    from pydantic import TypeAdapter
    from typing import Annotated
    from pydantic import StringConstraints

    TypeAdapter(
        Annotated[
            str,
            StringConstraints(
                min_length=3, max_length=64, pattern=r"^[a-z0-9][a-z0-9_.-]*$"
            ),
        ]
    ).validate_python(username)
    with SessionLocal.begin() as session:
        user = session.scalar(
            select(User)
            .where(User.username == username)
            .execution_options(include_deleted=True)
            .with_for_update()
        )
        if args.action == "create":
            if user is not None:
                raise ValueError("Username already exists, including deleted accounts.")
            role = args.role or input("Role (admin/provider): ").strip()
            if role not in {"admin", "provider"}:
                raise ValueError("Choose admin or provider.")
            session.add(
                User(username=username, role=role, password_hash=prompt_password())
            )
        else:
            if user is None or user.is_deleted:
                raise ValueError("Active account record not found.")
            if args.action == "reset-password":
                user.password_hash = prompt_password()
                user.failed_login_attempts = 0
                user.locked_until = None
            elif args.action == "set-role":
                if args.role is None:
                    raise ValueError("Specify --role admin or --role provider.")
                user.role = args.role
            elif args.action in {"disable", "enable"}:
                user.is_active = args.action == "enable"
            user.token_version += 1  # Invalidate all previously issued tokens.
    print("Account updated. No credentials were printed.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, IntegrityError):
        raise SystemExit(
            "Account update failed. Check input and account state; no changes committed."
        ) from None
