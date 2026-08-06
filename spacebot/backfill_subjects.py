#!/usr/bin/env python3
"""Name the subjects of documents ingested before subjects existed.

Run once after upgrading. Reads each workflow, asks the configured model who or what it is
about, and stores the answer. Everything ingested afterwards gets this for free during
ingestion.

Safe to re-run: it skips workflows that already have subjects unless --all is given, and it
never touches a workflow whose answer comes back empty or malformed — the statistical
extractor in sb/subjects.py continues to cover those, so a failed backfill degrades to
today's behaviour rather than to none.

    python3 backfill_subjects.py               name subjects for anything missing them
    python3 backfill_subjects.py --all         re-derive everywhere, merging with what's there
    python3 backfill_subjects.py --replace     re-derive and overwrite (discards existing)
    python3 backfill_subjects.py --dry-run     show what would change
"""
import json
import sys

from sb import db, retrieval
from sb.providers import get_provider, ProviderError

ASK = """Who or what is this material ABOUT?

Return STRICT JSON only: {"subjects": ["..."]}

Name people, companies, products or systems the material is about, as proper names, in the
document's own language and spelling. ABOUT, not merely mentioned:
- a CV is about the one person it belongs to, not about every employer it lists
- a vendor list is about each vendor in it
- a runbook is about the system it operates, not about whoever wrote it
- material that applies to everyone has no personal subject — return an empty list

Two or three entries is normal. An empty list is a valid and useful answer; a guessed name
is not, because this field decides which document a question is answered from.

MATERIAL:
"""


def material(pkg, limit=6000):
    parts = [pkg.get("name") or "", pkg.get("summary") or ""]
    for s in pkg.get("steps") or []:
        parts.append(f"{s.get('title', '')}. {s.get('body', '')}")
    for f in pkg.get("faqs") or []:
        parts.append(f"{f.get('question', '')} {f.get('answer', '')}")
    for a in pkg.get("extra_assets") or []:
        parts.append((a.get("text") or "")[:2000])
    return "\n".join(p for p in parts if p)[:limit]


def main():
    every = "--all" in sys.argv or "--replace" in sys.argv
    dry = "--dry-run" in sys.argv
    replace = "--replace" in sys.argv
    prov = get_provider()
    print(f"provider: {prov.name}\n")

    changed = 0
    for card in db.get_catalog():
        pkg = db.get_package(card["id"])
        if not pkg:
            continue
        if pkg.get("subjects") and not every:
            print(f"  skip  {pkg['wf_key']:24s} already has {pkg['subjects']}")
            continue

        try:
            raw = prov._chat(ASK, material(pkg), prov.route_model,
                             **({"deterministic": True} if prov.name == "ollama" else {}))
            subjects = json.loads(raw).get("subjects") if isinstance(raw, str) else \
                (raw or {}).get("subjects")
        except (ProviderError, ValueError, TypeError, AttributeError) as e:
            print(f"  FAIL  {pkg['wf_key']:24s} {type(e).__name__}: {e}")
            continue

        subjects = [str(s).strip() for s in (subjects or [])
                    if str(s).strip() and len(str(s).strip()) <= 80][:8]

        # Merge, matching what upsert_workflow does. One policy for this field, everywhere:
        # a model asked twice about the same document answers differently, so a re-derive
        # that replaces can silently drop a name an earlier run got right. --replace is
        # there for when you genuinely want to start the field over.
        merged = subjects
        if not replace:
            existing = pkg.get("subjects") or []
            seen = {s.casefold() for s in existing}
            merged = list(existing) + [s for s in subjects if s.casefold() not in seen
                                       and not seen.add(s.casefold())]
        merged = merged[:12]

        added = [s for s in merged if s not in (pkg.get("subjects") or [])]
        print(f"  ok    {pkg['wf_key']:24s} {merged}"
              f"{'   +' + str(added) if added and not replace else ''}")
        if merged and merged != (pkg.get("subjects") or []) and not dry:
            conn = db.connect()
            conn.execute("UPDATE workflows SET subjects=? WHERE id=?",
                         (json.dumps(merged), card["id"]))
            conn.commit()
            conn.close()
            changed += 1

    if changed and not dry:
        retrieval.invalidate()
    print(f"\n{changed} workflows updated{' (dry run)' if dry else ''}")


if __name__ == "__main__":
    db.init_db()
    main()
