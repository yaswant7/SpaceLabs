#!/usr/bin/env python3
"""An answer must be traceable to a place in a file, not just to the file.

The hierarchy is Source -> Segment -> Chunk -> Vector, and every level has to keep its
parent. Until now the last link was missing: the pipeline recorded {page: 7}, {para: 14},
{sheet, row} and {t_start, t_end} on every segment and then dropped all of it at chunking,
so a citation could name the document and never the place inside it.

Checks the links exist, survive retrieval, and reach the reader.
"""
import json
import sqlite3
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sb import chunks as cs, config, db, rag, retrieval   # noqa: E402

fails = []
KEY = "TEST.TRACE"


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}{'  ' + detail if detail else ''}")
    if not cond:
        fails.append(label)


db.init_db()

# A document as the adapters hand it over, with the anchors they really produce.
STRUCTURE = [
    {"text": "The maintenance window for the Zeltar cluster is 02:00 to 04:00 UTC on the "
             "first Sunday of each month.", "kind": "prose", "secret": False,
     "segment_id": "seg-alpha", "anchor": {"page": 7, "para": 2}},
    {"text": "Region: eu-west-1 | Cluster: zeltar-prod | Owner: platform",
     "kind": "table_row", "secret": False,
     "segment_id": "seg-beta", "anchor": {"sheet": "Clusters", "row": 12}},
    {"text": "In the recording the presenter explains the Zeltar failover drill.",
     "kind": "prose", "secret": False,
     "segment_id": "seg-gamma", "anchor": {"t_start": 252, "t_end": 278}},
]

db.upsert_workflow({
    "wf_key": KEY, "name": "Zeltar cluster notes", "summary":
        "Notes on the Zeltar cluster: its maintenance window, region and failover drill.",
    "category": "Test", "owner": "tester", "status": "published", "actor": "Test Harness",
    "steps": [], "known_errors": [], "faqs": [], "trigger_phrases": [],
})
card = next(c for c in db.get_catalog() if c["wf_key"] == KEY)

conn = sqlite3.connect(config.DB_PATH)
conn.execute(
    "INSERT INTO assets(id,workflow_id,kind,filename,text,source_ref,created_at) "
    "VALUES(?,?,?,?,?,?,?)",
    ("traceprobe", card["id"], "text", "runbook.pdf",
     "\n\n".join(s["text"] for s in STRUCTURE),
     json.dumps({"origin": "extracted", "segments": len(STRUCTURE),
                 "source_id": "src-zeltar", "structure": STRUCTURE}), 0.0))
conn.commit()
conn.close()

cs.reindex_workflow(card["id"])
cs.embed_pending()
retrieval.invalidate()

print("== chunks that came from a file keep their parents ==")
conn = sqlite3.connect(config.DB_PATH)
conn.row_factory = sqlite3.Row
rows = [dict(r) for r in conn.execute(
    "SELECT id,source,source_id,segment_id,anchor,text FROM chunks WHERE wf_key=?",
    (KEY,))]
conn.close()

# Only document chunks have a place in a file. A summary or an FAQ is written by the model
# about the whole document, so it has no page and must not claim one — asserting otherwise
# would demand a citation that would be a lie.
from_file = [r for r in rows if r["source"].startswith("document:")]
derived = [r for r in rows if not r["source"].startswith("document:")]

check("chunks were created", len(rows) >= 3, f"{len(rows)} chunks")
check("document chunks found", len(from_file) == 3, f"{len(from_file)} of {len(rows)}")
check("each names its source", all(r["source_id"] == "src-zeltar" for r in from_file),
      str({r["source_id"] for r in from_file}))
check("each names its segment",
      all((r["segment_id"] or "").startswith("seg-") for r in from_file),
      str({r["segment_id"] for r in from_file}))
anchors = [json.loads(r["anchor"] or "{}") for r in from_file]
check("each keeps an anchor", all(a for a in anchors), str(anchors))
check("model-written chunks claim no location",
      all(not json.loads(r["anchor"] or "{}") for r in derived),
      f"{len(derived)} derived chunk(s): {[r['source'] for r in derived]}")

print("\n== anchors read as a person would say them ==")
check("a page", rag._where({"page": 7, "para": 2}) == "page 7, paragraph 2",
      rag._where({"page": 7, "para": 2}))
check("a spreadsheet row", rag._where({"sheet": "Clusters", "row": 12})
      == "sheet Clusters, row 12", rag._where({"sheet": "Clusters", "row": 12}))
check("a timestamp", rag._where({"t_start": 252, "t_end": 278}) == "4:12–4:38",
      rag._where({"t_start": 252, "t_end": 278}))
check("a filename is kept alongside", "runbook.pdf" in rag._where({"page": 3}, "runbook.pdf"),
      rag._where({"page": 3}, "runbook.pdf"))
check("no anchor degrades to the filename", rag._where({}, "runbook.pdf") == "runbook.pdf")

print("\n== the location survives retrieval ==")
r = retrieval.retrieve("when is the Zeltar maintenance window")
mine = [c for c in r["chunks"] if c["wf_key"] == KEY]
check("the chunk was retrieved", bool(mine), f"{len(mine)} of {len(r['chunks'])}")
if mine:
    check("it still carries its anchor", bool(mine[0].get("anchor")),
          str(mine[0].get("anchor")))
    check("it still carries its segment", bool(mine[0].get("segment_id")),
          str(mine[0].get("segment_id")))

print("\n== and reaches the citation shown to the reader ==")
src = rag._sources(r)
mine_src = [s for s in src if s["wf_key"] == KEY]
check("the source is cited", bool(mine_src))
if mine_src:
    locs = mine_src[0].get("locations") or []
    check("with a location", bool(locs), str(locs))
    check("naming the page", any("page 7" in x for x in locs), str(locs))

db.delete_workflow(KEY)
retrieval.invalidate()

print(f"\n{'PASS' if not fails else 'FAIL'} — {len(fails)} failing")
for f in fails:
    print(f"  - {f}")
sys.exit(1 if fails else 0)
