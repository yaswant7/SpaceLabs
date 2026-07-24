"""CLI ask — quick way to test the pipeline without the web UI.

  python3 ask.py "the rollback failed saying lease held, what do I do?"
"""
import json
import sys

from sb import db
from sb.pipeline import ask


def render(a: dict):
    if a["abstained"]:
        print(f"\n🤔 {a['headline']}")
        if a.get("alternatives"):
            print("   Closest workflows:", ", ".join(x["name"] for x in a["alternatives"]))
        print(f"   [abstained · logged as a knowledge gap · provider={a['provider']}]")
        return
    badge = {"high": "🟢", "medium": "🟡", "low": "🔴"}.get(a["band"], "")
    wf = a["workflow"]
    print(f"\n{badge} {a['headline']}")
    print(f"   {wf['name']}  ·  {wf['wf_key']}  ·  verified by @{wf['verified_by']} ({wf['verified_at']})")
    print(f"   confidence {a['confidence']} · citation coverage {a['coverage']} · provider {a['provider']}")
    if a.get("clarify"):
        print(f"   ❓ {a['clarify']}")
    for b in a["blocks"]:
        if b["type"] == "steps":
            print()
            for i, s in enumerate(b["steps"], 1):
                print(f"   {i}. {s['title']}")
                print(f"      {s['body']}")
                if s.get("verification"):
                    print(f"      ✓ {s['verification']}")
                print(f"      [{s.get('cite','')}]")
        elif b["type"] == "known_error":
            print(f"\n   🔧 {b['code']}: {b['resolution']}   [{b.get('cite','')}]")
        elif b["type"] == "text":
            print(f"\n   {b['md']}   {b.get('cites', [])}")
    if a.get("alternatives"):
        print("\n   Alternatives:", ", ".join(x["name"] for x in a["alternatives"]))
    if a.get("followups"):
        print("   Follow-ups:", " | ".join(a["followups"]))


if __name__ == "__main__":
    db.init_db()
    q = " ".join(sys.argv[1:]) or "how do I roll back a production deploy?"
    print(f"Q: {q}")
    result = ask(q)
    render(result)
    if "--json" in sys.argv:
        print("\n" + json.dumps(result, indent=2))
