#!/usr/bin/env python3
"""Reset (or create) an HDC ERP login password from the command line.

Use this when the web login rejects the credentials you expect -- typically
because the database already contained an admin row created by an earlier
run with a different HDC_BOOTSTRAP_ADMIN_PASSWORD, or the password is simply
lost.

The bootstrap in hdc/core/bootstrap.py only creates the first admin when no
row for that username exists yet, so re-deploying with a new
HDC_BOOTSTRAP_ADMIN_PASSWORD will NOT change an existing password. This
script does.

Usage
-----
Show which database will be touched and which users exist::

    python3 scripts/reset_admin_password.py --show

Set the password for the default ``admin`` user::

    python3 scripts/reset_admin_password.py --username admin --password 'YourNewPass!123'

Read the password from stdin (nothing is echoed, nothing lands in shell
history)::

    python3 scripts/reset_admin_password.py --username admin --prompt

Promote a user to admin while you are there::

    python3 scripts/reset_admin_password.py --username admin --password 'YourNewPass!123' --role admin

Target a specific database explicitly (recommended on PythonAnywhere, where
the web app reads HDC_DB_PATH from production.env)::

    HDC_DB_PATH=/home/yourname/HDC_INSTANCE/hdc_erp.db \
      python3 scripts/reset_admin_password.py --username admin --prompt

Always run this with the same environment as the web app, otherwise you will
edit a different SQLite file than the one the site reads.
"""

import argparse
import getpass
import os
import sys

# Allow running as `python3 scripts/reset_admin_password.py` from the repo root.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from hdc.app import create_app                                     # noqa: E402
from hdc.config import get_runtime_settings                        # noqa: E402
from hdc.extensions import db                                      # noqa: E402
from hdc.models.auth import HDCUser                                # noqa: E402
from hdc.utils.format import _is_strong_password                   # noqa: E402


def _print_db_info(settings):
    print(f"instance_dir : {settings.instance_dir}")
    print(f"db_path      : {settings.db_path}")
    exists = os.path.exists(settings.db_path)
    print(f"db exists    : {exists}")
    if not exists:
        print("note         : the file will be created and bootstrapped on first use")


def _print_users():
    users = HDCUser.query.order_by(HDCUser.id).all()
    if not users:
        print("users        : none")
        return
    print(f"users        : {len(users)}")
    for u in users:
        print(f"  - id={u.id} username={u.username!r} role={u.role!r}")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--username', default=os.environ.get('HDC_BOOTSTRAP_ADMIN_USERNAME', 'admin'))
    parser.add_argument('--password', help='New password (must pass the app strength rules).')
    parser.add_argument('--prompt', action='store_true', help='Read the new password from stdin instead.')
    parser.add_argument('--role', choices=['admin', 'manager', 'user'],
                        help='Also set the user role (default: leave as-is, "admin" for a new user).')
    parser.add_argument('--show', action='store_true',
                        help='Only print the database path and existing users, then exit.')
    args = parser.parse_args()

    app = create_app()
    settings = get_runtime_settings()
    _print_db_info(settings)

    with app.app_context():
        if args.show:
            _print_users()
            return 0

        if args.prompt:
            password = getpass.getpass(f"New password for {args.username!r}: ")
            confirm = getpass.getpass("Repeat new password: ")
            if password != confirm:
                print("error        : the two passwords do not match; nothing changed.")
                return 2
        else:
            password = args.password or ''

        if not password:
            print("error        : provide --password or --prompt.")
            return 2

        ok, msg = _is_strong_password(password)
        if not ok:
            print(f"error        : {msg}")
            return 2

        from werkzeug.security import generate_password_hash

        username = args.username.strip()
        user = HDCUser.query.filter_by(username=username).first()
        if user is None:
            role = args.role or 'admin'
            user = HDCUser(username=username,
                           password_hash=generate_password_hash(password),
                           role=role)
            db.session.add(user)
            action = 'created'
        else:
            user.password_hash = generate_password_hash(password)
            action = 'password updated'
            if args.role:
                user.role = args.role
                action += f" (role -> {args.role})"
        db.session.commit()

        print(f"result       : {action} for {username!r} in {settings.db_path}")
        print("next         : restart/reload the web app, then log in with these credentials.")
        return 0


if __name__ == '__main__':
    sys.exit(main())
