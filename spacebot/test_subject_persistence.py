#!/usr/bin/env python3
"""Subjects must survive an update that doesn't mention them.

This is the failure mode that hides. A re-ingest, an edit in the Studio, any writer that
predates the subjects column — each arrives at upsert_workflow with an empty list, and an
unconditional write turns that into "this document is about nobody". Nothing errors.
Retrieval keeps working. It just quietly loses the attribution that stops one person's
question being answered from another person's file, and the only symptom is a conflation
weeks later.

Found for real: test_formats re-ingests demo documents, and after one run the subjects for
every person in the corpus — Meena, Sarah, Arjun, Priya — had gone to zero.
"""
import json
import sqlite3
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sb import config, db   # noqa: E402

fails = []
KEY = "TEST.SUBJECT_PERSISTENCE"


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}{'  ' + detail if detail else ''}")
    if not cond:
        fails.append(label)


def subjects_of(wf_key):
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    r = conn.execute("SELECT subjects FROM workflows WHERE wf_key=?", (wf_key,)).fetchone()
    conn.close()
    return json.loads((r["subjects"] if r else "") or "[]")


db.init_db()
base = {"wf_key": KEY, "name": "Persistence probe", "summary": "probe",
        "category": "Test", "owner": "tester", "steps": [], "known_errors": [], "faqs": []}

print("== a writer that knows about subjects sets them ==")
db.upsert_workflow({**base, "subjects": ["Amara Okonkwo", "Norvale Logistics"]})
check("subjects stored", subjects_of(KEY) == ["Amara Okonkwo", "Norvale Logistics"],
      str(subjects_of(KEY)))

print("\n== a writer that doesn't must not erase them ==")
db.upsert_workflow({**base, "summary": "edited elsewhere"})      # no subjects key at all
check("subjects survive an unrelated update", subjects_of(KEY) == ["Amara Okonkwo",
                                                                   "Norvale Logistics"],
      str(subjects_of(KEY)))

db.upsert_workflow({**base, "subjects": []})                     # explicit empty list
check("subjects survive an empty list", subjects_of(KEY) == ["Amara Okonkwo",
                                                             "Norvale Logistics"],
      str(subjects_of(KEY)))

print("\n== a later write adds, it does not replace ==")
# Model extraction is not stable run to run: re-ingesting the on-call rota once returned
# the document's title instead of the five people in it. Accumulating means one forgetful
# run cannot delete an attribution that a previous run got right.
db.upsert_workflow({**base, "subjects": ["Trellis Health"]})
check("new subject added", "Trellis Health" in subjects_of(KEY), str(subjects_of(KEY)))
check("earlier subjects kept", "Amara Okonkwo" in subjects_of(KEY), str(subjects_of(KEY)))
check("no duplicates on repeat", (db.upsert_workflow({**base, "subjects": ["Trellis Health"]})
                                  or subjects_of(KEY).count("Trellis Health") == 1),
      str(subjects_of(KEY)))

print("\n== the live corpus still knows who its documents are about ==")
have = [c["wf_key"] for c in db.get_catalog() if db.get_package(c["id"]).get("subjects")]
check("most workflows carry subjects", len(have) >= max(1, len(db.get_catalog()) // 2),
      f"{len(have)}/{len(db.get_catalog())}")

conn = sqlite3.connect(config.DB_PATH)
conn.execute("DELETE FROM workflows WHERE wf_key=?", (KEY,))
conn.commit()
conn.close()

print(f"\n{'PASS' if not fails else 'FAIL'} — {len(fails)} failing")
for f in fails:
    print(f"  - {f}")
sys.exit(1 if fails else 0)
