#!/usr/bin/env python3
"""First-run setup for a real deployment. Creates an admin account and names the install.

This exists because the only other way to get a login was `seed.py`, which also loads a
demo corpus belonging to somebody else. Anyone standing this up for their own team had a
choice between importing another company's documents or writing users into SQLite by hand.
Those are now separate: this script gives you an empty, usable install, and `seed.py`
remains the demo.

    python3 setup.py --org "Acme Logistics" --admin you@acme.com
    python3 setup.py --org "Acme Logistics" --admin you@acme.com --password "..."

With no --password, one is generated and printed once. Passwords are never defaulted: a
known password on an internal knowledge base is a door, and the demo's fixed logins exist
only because they are throwaway.

Nothing here reaches the network. Setup writes to the local SQLite file and nothing else.
"""
import argparse
import secrets
import sys

from sb import auth, db


def main():
    p = argparse.ArgumentParser(description="Prepare an empty Spacebot install.")
    p.add_argument("--org", required=True,
                   help="your organisation's name, as the assistant should say it")
    p.add_argument("--admin", required=True, help="email for the first admin account")
    p.add_argument("--name", default="", help="that admin's display name")
    p.add_argument("--password", default="", help="omit to have one generated")
    p.add_argument("--assistant-name", default="Spacebot",
                   help="what the assistant calls itself")
    args = p.parse_args()

    db.init_db()

    generated = ""
    password = args.password
    if not password:
        generated = secrets.token_urlsafe(12)
        password = generated

    auth.ensure_user(args.admin, args.name or args.admin.split("@")[0], "admin", password)
    db.set_setting("org_name", args.org)
    db.set_setting("assistant_name", args.assistant_name)

    print(f"\n  organisation : {args.org}")
    print(f"  assistant    : {args.assistant_name}")
    print(f"  admin        : {args.admin}")
    if generated:
        print(f"  password     : {generated}      <- shown once, store it now")
    print(f"\n  workflows    : {len(db.list_workflows())} (an empty install answers nothing "
          f"until you add documents)")
    print("\nNext:")
    print("  python3 ingest.py <folder>      add your own documents")
    print("  python3 server.py               http://localhost:8080")
    print("\nModel settings (provider, local or hosted) are on the admin page, or in .env.")


if __name__ == "__main__":
    sys.exit(main())
