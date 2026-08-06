#!/usr/bin/env python3
"""Nothing from OUR corpus may leak into a NEW customer's ingested document.

The product test, not a feature test. Someone clones this repo, points it at their own
files, and every name they see must be theirs. Shipped prompts carry worked examples, and
this codebase has already been bitten twice by a small model reproducing an example
verbatim instead of following its shape — so an example naming a real person is a defect
waiting to be someone else's summary.

Ingests a CV for a person who does not exist in this corpus and asserts that no name from
the demo data appears anywhere in what the model wrote about them.
"""
import sys
import sqlite3
import time

sys.path.insert(0, "/home/yaswant7/projects/SpaceLabs/spacebot")
from sb import config, db          # noqa: E402
from sb.media import pipeline as mpipe   # noqa: E402

# Names that exist only in the shipped prompts or the demo corpus. None may appear.
FOREIGN = ["yaswanth", "kamineni", "jntuk", "spacelabs", "meena", "acme", "globex",
           "initech", "modernsignal", "modern signal", "arjun", "priya", "sreedhar"]

CV = """Amara Okonkwo — Curriculum Vitae

Amara Okonkwo is a data engineer with six years of experience building batch and streaming
pipelines. She currently works at Norvale Logistics in Lisbon, where she leads the data
platform team.

Experience. At Norvale Logistics she rebuilt the nightly reconciliation job on Apache Beam,
cutting runtime from nine hours to forty minutes. Before that she worked at Trellis Health
on patient record ingestion.

Skills. Python, Go, Apache Beam, dbt, BigQuery, Terraform.

Education. Amara studied Computer Science at the University of Porto, graduating in 2018.

Contact. amara.okonkwo@example.com
"""

db.init_db()
KEY = "TEST.FOREIGN_CV"

job = mpipe.start_ingest(KEY, "", "Amara CV", "tester",
                         [{"filename": "amara_cv.txt",
                           "mime": "text/plain",
                           "bytes": CV.encode()}])
for _ in range(240):
    j = db.get_job(job)
    if j["status"] in ("drafted", "failed"):
        break
    time.sleep(1)

j = db.get_job(job)
print(f"ingest status: {j['status']}")
if j.get("error"):
    print(f"  error: {j['error'][:160]}")

row = None
conn = sqlite3.connect(config.DB_PATH)
conn.row_factory = sqlite3.Row
r = conn.execute("SELECT id FROM workflows WHERE wf_key=?", (KEY,)).fetchone()
if r:
    row = db.get_package(r["id"])

fails = []
if not row:
    fails.append("ingest produced no workflow at all")
else:
    blob = " ".join([
        row.get("name") or "", row.get("summary") or "",
        " ".join(t for t in (row.get("trigger_phrases") or [])),
        " ".join(f"{s.get('title','')} {s.get('body','')}" for s in row.get("steps") or []),
        " ".join(f"{f.get('question','')} {f.get('answer','')}"
                 for f in row.get("faqs") or []),
    ]).lower()

    print(f"\nname:    {row.get('name')}")
    print(f"summary: {(row.get('summary') or '')[:220]}")

    for n in FOREIGN:
        if n in blob:
            fails.append(f"leaked a name from our own corpus/prompts: {n!r}")
    if "amara" not in blob and "okonkwo" not in blob:
        fails.append("never names the actual subject of the document")

# clean up
if r:
    for t in ("steps", "known_errors", "faqs", "assets"):
        conn.execute(f"DELETE FROM {t} WHERE workflow_id=?", (r["id"],))
    conn.execute("DELETE FROM workflows WHERE id=?", (r["id"],))
    conn.commit()
conn.close()

print(f"\n{'PASS' if not fails else 'FAIL'} — {len(fails)} problems")
for f in fails:
    print(f"  - {f}")
sys.exit(1 if fails else 0)
