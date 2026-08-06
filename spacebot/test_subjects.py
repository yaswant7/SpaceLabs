#!/usr/bin/env python3
"""Subject attribution: does retrieval know whose knowledge each chunk is?

Two halves, and the second matters as much as the first.

  ABSTAIN   a question about someone the corpus has never heard of must not be answered
            out of whoever's document ranked highest. This is the reported bug: "any info
            about sreedhar masula?" was answered from Yaswanth's CV.

  NO HARM   every question that worked before must still work. A subject gate is a filter
            on evidence, and a filter that is even slightly too eager silently converts a
            working knowledge base into one that says "I don't have that" — a far worse
            failure than the one it fixes, because nobody reports it as a bug.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sb import retrieval, subjects   # noqa: E402

fails = []


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}{'  ' + detail if detail else ''}")
    if not cond:
        fails.append(label)


def abstains(q):
    r = retrieval.retrieve(q)
    return bool(r.get("unsupported_terms") or r.get("unknown_subjects")
                or r.get("subject_miss")), r


print("== a person nobody has documented ==")
for q in ["you know any info about sreedhar masula?",
          "who is sreedhar masula",
          "what is sreedhar masula's current role",
          "tell me about ravi teja anand"]:
    stop, r = abstains(q)
    check(q, stop, f"unknown={r.get('unknown_subjects')}")

print("\n== questions that must keep working ==")
WORKING = [
    "what is yaswanth's current role",
    "how do I roll back a production deploy",
    "which vendors has meena approved",
    "who is on call in week 32",
    "what is the hotel limit on expenses",
    "how do I create a purchase order",
    "how do I set up my local dev environment",
    "how do I connect to the vpn",
    "what do I need before my first on-call shift",
    "how do I request aws access",
    "what programming languages does yaswanth know",
    "when was modern signal founded",
]
for q in WORKING:
    stop, r = abstains(q)
    check(q, not stop,
          f"ev={r['evidence']} unknown={r.get('unknown_subjects')} "
          f"miss={r.get('subject_miss')}")

print("\n== the named subject outranks the look-alike document ==")
# A CV and an on-call rota are the same shape to every retriever we run: short factual
# lines about people. Naming the person has to be what breaks the tie.
r = retrieval.retrieve("what is yaswanth's current role")
top = r["chunks"][0] if r["chunks"] else {}
check("yaswanth's CV ranks first", top.get("wf_key") == "PEOPLE.YASWANTH_CV",
      f"got {top.get('wf_key')}")
check("top chunk is attributed to him", "yaswanth" in (top.get("subjects") or []),
      f"subjects={top.get('subjects')}")

print("\n== every excerpt reaches the model attributed ==")
r = retrieval.retrieve("which vendors has meena approved")
ctx = retrieval.context_for_prompt(r)
check("context carries an 'about' line", "These excerpts are about:" in ctx)
unattributed = [c["chunk_id"] for c in r["chunks"] if not c.get("subjects")]
check("no chunk is unattributed", not unattributed, f"{len(unattributed)} bare")

print("\n== extraction quality ==")
sidx = retrieval._subject_index()
gate = sidx["gate"]
for name in ["yaswanth", "meena", "acme", "tailscale", "pagerduty"]:
    check(f"'{name}' is a gating subject", name in gate)
for word in ["create", "status", "policy", "week", "owner", "machine"]:
    check(f"'{word}' is NOT a gating subject", word not in gate)

print("\n== a tokeniser artefact is not an unknown entity ==")
# Measured: 'call', '32', 'limit' and 'expenses' are all absent from the lexical index,
# because "on-call" tokenises whole and the corpus never writes "expenses" unpluralised.
# Treating corpus-absence alone as an unknown entity would abstain on all of these.
for q in ["who is on call in week 32", "what is the hotel limit on expenses"]:
    r = retrieval.retrieve(q)
    check(f"no false unknown: {q}", not r.get("unknown_subjects"),
          f"{r.get('unknown_subjects')}")

print(f"\n{'PASS' if not fails else 'FAIL'} — {len(fails)} failing")
sys.exit(1 if fails else 0)
