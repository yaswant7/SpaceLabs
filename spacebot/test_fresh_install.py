#!/usr/bin/env python3
"""The clone-it-and-use-it path, end to end, on a throwaway database.

Someone downloads this, runs setup, points ingest at a folder and asks a question. That
whole sequence has to work without touching the demo corpus and without any trace of it —
which is a different claim from "the demo works", and the only one that matters to a second
deployment.

Run with SPACEBOT_DB pointing somewhere disposable:
    SPACEBOT_DB=/tmp/fresh/fresh.db python3 test_fresh_install.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sb import config, db, prompts, rag   # noqa: E402

fails = []


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}{'  ' + detail if detail else ''}")
    if not cond:
        fails.append(label)


print(f"database: {config.DB_PATH}")
if "fresh" not in config.DB_PATH:
    print("refusing to run against the demo database — set SPACEBOT_DB first")
    sys.exit(2)

print("\n== the install is the deployer's, not ours ==")
opening = prompts.render(prompts.RAG_SYSTEM).split("\n")[0]
check("assistant introduces the deploying org", "Northwind Freight" in opening, opening[:70])
check("assistant uses the configured name", "Wren" in opening)

keys = {w["wf_key"] for w in db.list_workflows()}
check("only the deployer's documents are present", all(not k.startswith(
    ("DEPLOY.", "PROC.", "ONCALL.", "OPS.", "PEOPLE.", "POLICY.", "ACCESS.", "ENV.",
     "IT.", "MODERNSIGNAL.")) for k in keys), str(sorted(keys)))

print("\n== it answers from the deployer's own document ==")
out = rag.answer("where do I park my bike?", profile="a new starter")
print(f"  -> {out['answer'][:260]}")
low = out["answer"].lower()
check("does not abstain", not out["abstained"], f"band={out['band']}")
check("answers from the ingested document",
      any(w in low for w in ("rack", "north", "loading bay", "gated", "chandler")))

print("\n== no trace of the corpus it was built against ==")
for n in ["spacelabs", "yaswanth", "meena", "acme", "globex", "argo", "pagerduty",
          "tailscale", "sap"]:
    if n in low:
        fails.append(f"leaked {n!r} from the demo corpus")
check("answer is free of demo-corpus names", not fails)

print("\n== and it still declines what it does not hold ==")
out2 = rag.answer("how do I roll back a production deploy?", profile="a new starter")
print(f"  -> {out2['answer'][:200]}")
check("abstains on a topic this install has no document for", out2["abstained"],
      f"band={out2['band']}")

print(f"\n{'PASS' if not fails else 'FAIL'} — {len(fails)} failing")
for f in fails:
    print(f"  - {f}")
sys.exit(1 if fails else 0)
