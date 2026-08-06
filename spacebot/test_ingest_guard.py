#!/usr/bin/env python3
"""The regression that matters most: unreadable input must FAIL the job, never become
a published workflow. This is the exact path that invented 'PDF Parsing with PyPDF'."""
import sys
import time

sys.path.insert(0, "/home/yaswant7/projects/SpaceLabs/spacebot")
from sb import db  # noqa: E402
from sb.media import pipeline as mpipe  # noqa: E402

db.init_db()
fails = 0


def run(label, files, expect_status):
    global fails
    key = "TEST." + label.upper().replace(" ", "_")
    job = mpipe.start_ingest(key, "", "Test", "tester", files)
    # Generous, because this waits on a local model structuring a whole document on CPU.
    # Measured at 253s for a short runbook on llama3.2:3b, so a 180s poll reported a
    # perfectly good ingest as a failure — and a timeout dressed as a failed guard is the
    # most misleading result this suite can produce.
    for _ in range(600):
        j = db.get_job(job)
        if j["status"] in ("drafted", "failed"):
            break
        time.sleep(1)
    j = db.get_job(job)

    made = any(w["wf_key"] == key for w in db.list_workflows())
    ok = j["status"] == expect_status and (made == (expect_status == "drafted"))
    if not ok:
        fails += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {label:26} status={j['status']:8} "
          f"workflow_created={made}")
    if j.get("error"):
        print(f"       reason: {j['error'][:96]}")

    # clean up anything a passing case created
    if made:
        import sqlite3
        from sb import config
        c = sqlite3.connect(config.DB_PATH)
        c.row_factory = sqlite3.Row
        r = c.execute("SELECT id FROM workflows WHERE wf_key=?", (key,)).fetchone()
        if r:
            for t in ("steps", "known_errors", "faqs", "assets"):
                c.execute(f"DELETE FROM {t} WHERE workflow_id=?", (r["id"],))
            c.execute("DELETE FROM workflows WHERE id=?", (r["id"],))
            c.commit()
        c.close()


print("unreadable / thin input must not become a workflow")
run("corrupt pdf", [{"filename": "broken.pdf", "mime": "application/pdf",
                     "bytes": b"%PDF-1.4 this is not really a pdf"}], "failed")
run("empty text", [{"filename": "notes.txt", "mime": "text/plain", "bytes": b"   "}], "failed")
run("one thin line", [{"filename": "notes.txt", "mime": "text/plain",
                       "bytes": b"[PDF parsing needs pypdf]"}], "failed")

print("\nreal material still works")
run("real runbook", [{"filename": "runbook.txt", "mime": "text/plain", "bytes": b"""
Rotating the signing certificate

Before you start, make sure you are on the VPN and have access to the vault.

Step one: fetch the current certificate with `vaultctl cert get --env prod`. You should
see the expiry date printed at the top of the output.

Step two: generate a replacement with `vaultctl cert rotate --env prod --ttl 90d`. This
prints a new fingerprint. Copy it somewhere safe, it is only shown once.

Step three: update the load balancer with `lbctl set-cert --fingerprint <value>` and wait
for the health checks to go green. If they stay red for more than two minutes, roll back
with `lbctl rollback-cert`.

A common mistake is skipping the VPN check, which makes the vault calls time out with a
confusing DNS error rather than an auth error.
"""}], "drafted")

print(f"\n{4 - fails}/4 passed")
sys.exit(1 if fails else 0)
