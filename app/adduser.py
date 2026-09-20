"""Make or update a Platen user from the command line.

    python -m app.adduser dave --role operator
"""

from __future__ import annotations

import argparse
import getpass
import sys

from db import session as dbsession
from db.models import AppUser

from . import auth


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.adduser")
    parser.add_argument("username")
    parser.add_argument("--role", choices=auth.ROLES, default="operator")
    parser.add_argument("--name", default="", help="how to show them in the sidebar")
    args = parser.parse_args(argv)

    password = getpass.getpass("Password: ")
    if password != getpass.getpass("Again: "):
        print("those didn't match", file=sys.stderr)
        return 1

    dbsession.engine()
    with dbsession.SessionLocal() as s:
        existing = s.get(AppUser, args.username.strip().lower())
        try:
            if existing is not None:
                auth.set_password(s, existing, password)
                existing.role = args.role
                if args.name:
                    existing.display_name = args.name
                s.commit()
                print(f"updated {existing.username} ({existing.role})")
            else:
                user = auth.create_user(s, args.username, password,
                                        role=args.role, display_name=args.name)
                print(f"made {user.username} ({user.role})")
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
