"""CLI ask — exercise the RAG pipeline without the web UI.

  python3 ask.py "the rollback failed saying lease held, what do I do?"
  python3 ask.py --json "who is on call in week 2026-W32?"
"""
import json
import sys

from sb import chunks, db
from sb.rag import answer

if __name__ == "__main__":
    db.init_db()
    chunks.init()
    q = " ".join(a for a in sys.argv[1:] if not a.startswith("--")) \
        or "how do I roll back a production deploy?"
    print(f"Q: {q}\n")

    a = answer(q)
    print(a["answer"])

    r = a.get("retrieval") or {}
    srcs = ", ".join(s["wf_key"] for s in (a.get("sources") or [])) or "(none)"
    print("\n" + "-" * 70)
    print(f"policy     : {r.get('policy')}   evidence: {a.get('confidence')}   band: {a.get('band')}")
    print(f"chunks     : {r.get('chunks')}   semantic: {r.get('semantic')}   provider: {a.get('provider')}")
    print(f"sources    : {srcs}")
    if a.get("followups"):
        print("follow-ups : " + " | ".join(a["followups"]))
    if a.get("degraded"):
        print(f"DEGRADED   : {a['degraded']}")
    if "--json" in sys.argv:
        print("\n" + json.dumps(a, indent=2))
