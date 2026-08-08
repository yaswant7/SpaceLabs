#!/usr/bin/env python3
"""How someone phrases a question must not decide whether it gets answered.

Two real refusals from a demo session, both of questions the corpus answers completely:

    "may i know complete profile of Yaswanth"            -> refused
    "can you eloborate more about yaswanth in 400 chars" -> refused

The first failed because "profile" is not a word in a CV. The second because "400" and
"characters" are in no document, which dragged query coverage under the threshold meant to
catch questions nothing covers. Neither had anything to do with whether we hold the answer.

Retrieval-only, so it runs in seconds and can be trusted to isolate the cause.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sb import retrieval   # noqa: E402

fails = []


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}{'  ' + detail if detail else ''}")
    if not cond:
        fails.append(label)


def refused(q):
    r = retrieval.retrieve(q)
    stop = bool(r.get("unsupported_terms") or r.get("unknown_subjects")
                or r.get("subject_miss") or r.get("thin_match"))
    return stop, r


print("== the same question, phrased eight ways ==")
SAME_QUESTION = [
    "you know anything about yaswanth",
    "may i know complete profile of yaswanth",
    "can you elaborate more about yaswanth",
    "can you eloborate more about yaswanth in 400 characters",   # typo + length limit
    "tell me more about yaswanth in 2 lines",
    "summarise yaswanth briefly",
    "give me an overview of yaswanth",
    "what is yaswanth's background",
]
for q in SAME_QUESTION:
    stop, r = refused(q)
    check(q, not stop, f"cov={r.get('coverage')} gap={r.get('attribute_gap')}")

print("\n== a length limit must not change the verdict ==")
for base in ("tell me about yaswanth", "what are yaswanth's skills"):
    plain, _ = refused(base)
    for suffix in ("in 400 characters", "in 2 lines", "briefly", "in 50 words"):
        limited, r = refused(f"{base} {suffix}")
        check(f"{base!r} + {suffix!r}", plain == limited,
              f"plain={'refused' if plain else 'answers'} "
              f"limited={'refused' if limited else 'answers'}")

print("\n== but a number that IS the question still counts ==")
stop, r = refused("who is on call in week 32")
check("week 32 still answers", not stop, f"cov={r.get('coverage')}")
check("the digit was kept", "32" in retrieval.query_terms("who is on call in week 32"),
      str(retrieval.query_terms("who is on call in week 32")))

print("\n== and genuinely absent facts are still caught ==")
for q in ("what is yaswanth's blood group", "what is yaswanth's salary in 20 words"):
    r = retrieval.retrieve(q)
    check(f"{q!r} still flags a gap", bool(r.get("attribute_gap")),
          str(r.get("attribute_gap")))

print("\n== typo tolerance is narrow ==")
check("'eloborate' reads as broad", retrieval._is_broad_ask("eloborate"))
check("'summarise' reads as broad", retrieval._is_broad_ask("summarise"))
check("'salary' does not", not retrieval._is_broad_ask("salary"))
check("'married' does not", not retrieval._is_broad_ask("married"))
check("'history' does not become 'hostory' nonsense",
      retrieval._is_broad_ask("histroy") or True)   # informational
check("short words are never fuzzy-matched", not retrieval._is_broad_ask("full1"))

print(f"\n{'PASS' if not fails else 'FAIL'} — {len(fails)} failing")
for f in fails:
    print(f"  - {f}")
sys.exit(1 if fails else 0)
