#!/usr/bin/env python3
"""Clear usage history and the audit trail. Knowledge and logins are untouched.

Development leaves a mess in exactly the tables the admin screen reads from: every test run
logs questions, every fixture logs an edit. That turns "446 questions, 65% answered, 171
open gaps" into a number that describes the test suite rather than the team, and fills the
change log with `edited category` a dozen times.

Use before a demo, or whenever the activity data stops describing real use.

    python3 tools/reset_activity.py --yes
"""
import sqlite3
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sb import config, db   # noqa: E402

TABLES = ["ask_log", "gaps", "audit_log"]

db.init_db()
conn = sqlite3.connect(config.DB_PATH)
counts = {}
for t in TABLES:
    try:
        counts[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    except sqlite3.OperationalError:
        counts[t] = 0

print("would clear:")
for t, n in counts.items():
    print(f"  {t:<12} {n} rows")
print("\nknowledge, users and conversations are NOT touched.")

if "--yes" not in sys.argv:
    print("\nre-run with --yes to actually clear.")
    conn.close()
    sys.exit(0)

for t in TABLES:
    try:
        conn.execute(f"DELETE FROM {t}")
    except sqlite3.OperationalError:
        pass
conn.commit()
conn.close()
print("\ncleared.")
