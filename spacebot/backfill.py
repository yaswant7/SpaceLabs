"""One-time: give workflows that were ingested with no trigger phrases something to route
on, derived from their own name/summary/step text. Safe to re-run."""
import json
import re
import sqlite3
from collections import Counter

from sb import config

WORD = re.compile(r"[a-z0-9_]+")
STOP = {"the", "and", "with", "that", "this", "your", "from", "into", "when", "then",
        "there", "which", "about", "would", "should", "these", "those", "their"}

c = sqlite3.connect(config.DB_PATH)
c.row_factory = sqlite3.Row
for r in c.execute("SELECT id, wf_key, name, summary, trigger_phrases FROM workflows").fetchall():
    if json.loads(r["trigger_phrases"] or "[]"):
        continue                                   # already has triggers — leave it
    steps = c.execute("SELECT title || ' ' || body AS t FROM steps WHERE workflow_id=?", (r["id"],)).fetchall()
    text = " ".join([r["name"] or "", r["summary"] or ""] + [s["t"] for s in steps]).lower()
    toks = [t for t in WORD.findall(text) if len(t) >= 5 and t not in STOP]
    triggers = [w for w, _ in Counter(toks).most_common(10)]
    c.execute("UPDATE workflows SET trigger_phrases=? WHERE id=?", (json.dumps(triggers), r["id"]))
    print("backfilled", r["wf_key"], "->", triggers[:6])
c.commit()
c.close()
print("done")
