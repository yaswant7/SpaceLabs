#!/usr/bin/env python3
"""Document structure must survive all the way to a chunk.

The adapters work hard to recover it — a DOCX heading stays a heading, a spreadsheet
becomes one segment per row, an HTML nav bar is dropped — and until now the pipeline welded
every segment into one string before chunking, so all of it died one step before it was
used. A table then got split on character count, which cuts rows in half, and nothing
downstream knew a table was involved.

Also checks the flag that was being computed and ignored: segments the ingest step marks as
holding a credential must not reach the index at all.
"""
import json
import sys
import os
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sb import chunks as cs, config, db, retrieval   # noqa: E402

fails = []
KEY = "TEST.STRUCTURE"


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}{'  ' + detail if detail else ''}")
    if not cond:
        fails.append(label)


db.init_db()

# A document as the adapters hand it over: rows of a table, a paragraph of prose, and one
# segment the secret scanner flagged.
STRUCTURE = [
    {"text": "Vendor: Acme Ltd | Status: approved | Owner: Meena", "kind": "table_row",
     "secret": False},
    {"text": "Vendor: Globex | Status: pending | Owner: Meena", "kind": "table_row",
     "secret": False},
    {"text": "Vendor: Initech | Status: approved | Owner: Raj", "kind": "table_row",
     "secret": False},
    {"text": ("Suppliers must be approved before a purchase order can be raised against "
              "them. Approval is handled in SAP by the procurement team and usually takes "
              "two working days from submission."), "kind": "prose", "secret": False},
    {"text": "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
     "kind": "prose", "secret": True},
]

db.upsert_workflow({
    "wf_key": KEY, "name": "Structure probe", "summary": "probe", "category": "Test",
    "owner": "tester", "status": "published", "actor": "Test Harness",
    "steps": [], "known_errors": [], "faqs": [], "trigger_phrases": [],
})
card = next(c for c in db.get_catalog() if c["wf_key"] == KEY)
conn = sqlite3.connect(config.DB_PATH)
conn.execute(
    "INSERT INTO assets(id,workflow_id,kind,filename,text,source_ref,created_at) "
    "VALUES(?,?,?,?,?,?,?)",
    ("structprobe", card["id"], "text", "vendors.xlsx",
     "\n\n".join(s["text"] for s in STRUCTURE),
     json.dumps({"origin": "extracted", "segments": len(STRUCTURE),
                 "structure": STRUCTURE}), 0.0))
conn.commit()
conn.close()

cs.reindex_workflow(card["id"])
rows = [r for r in cs.all_chunks() if r["wf_key"] == KEY]

print("== the table survives as rows ==")
conn = sqlite3.connect(config.DB_PATH)
conn.row_factory = sqlite3.Row
meta = {r["id"]: json.loads(r["meta"] or "{}")
        for r in conn.execute("SELECT id,meta FROM chunks WHERE wf_key=?", (KEY,))}
conn.close()

kinds = [meta.get(r["id"], {}).get("kind") for r in rows]
check("chunk.meta is populated at all", any(kinds), f"kinds={sorted(set(k for k in kinds if k))}")
check("table rows are tagged", kinds.count("table_row") == 3,
      f"{kinds.count('table_row')} of 3")

# Each vendor should be findable on its own — the point of row-level chunks.
for vendor in ("Acme", "Globex", "Initech"):
    hits = [r for r in rows if vendor in r["text"]]
    check(f"{vendor} is in exactly one chunk", len(hits) == 1, f"{len(hits)} chunks")

print("\n== a flagged credential never reaches the index ==")
leaked = [r for r in rows if "wJalrXUtnFEMI" in r["text"] or "AWS_SECRET" in r["text"]]
check("the secret segment was skipped", not leaked, f"{len(leaked)} chunks contain it")
check("everything else still indexed", len(rows) >= 4, f"{len(rows)} chunks")

print("\n== retrieval tells the model it is reading a table ==")
cs.embed_pending()
retrieval.invalidate()
r = retrieval.retrieve("is Globex an approved vendor")
ctx = retrieval.context_for_prompt(r)
got_row = any((c.get("meta") or {}).get("kind") in ("table_row", "table", "sheet")
              for c in r["chunks"])
check("a table row was retrieved", got_row)
if got_row:
    check("the context labels it", "one row from a table" in ctx,
          ctx[:80].replace("\n", " "))

# clean up
db.delete_workflow(KEY)
retrieval.invalidate()

print(f"\n{'PASS' if not fails else 'FAIL'} — {len(fails)} failing")
for f in fails:
    print(f"  - {f}")
sys.exit(1 if fails else 0)
