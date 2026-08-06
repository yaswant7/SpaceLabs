#!/usr/bin/env python3
"""Point the demo logins at the new names without reseeding the knowledge base.

`seed.py` would also reload the demo workflows, which by now carry model-extracted subjects
and audit history worth keeping. This touches users only.
"""
import sqlite3
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sb import auth, config, db   # noqa: E402

NEW = [
    ("yaswanth@spacelabs.dev", "Yaswanth (new hire)", "user", "yaswanth123"),
    ("roshan@spacelabs.dev", "Roshan (senior)", "author", "roshan123"),
    ("admin@spacelabs.dev", "Admin", "admin", "admin123"),
]
RETIRED = ["raj@spacelabs.dev", "sarah@spacelabs.dev"]

db.init_db()
for email, name, role, pw in NEW:
    auth.ensure_user(email, name, role, pw)
    print(f"  {role:6} {email}  /  {pw}")

conn = sqlite3.connect(config.DB_PATH)
conn.row_factory = sqlite3.Row
for email in RETIRED:
    row = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
    if row:
        # Sessions go too, or a browser still holding one stays signed in as a user that
        # no longer exists.
        conn.execute("DELETE FROM sessions WHERE user_id=?", (row["id"],))
        conn.execute("DELETE FROM users WHERE id=?", (row["id"],))
        print(f"  retired {email}")
conn.commit()
conn.close()
print("\ndone")
