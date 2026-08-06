#!/usr/bin/env python3
"""Does the subject guard actually do the work, or would these questions abstain anyway?

Retrieval-only, so it runs in a second and needs no generation. It disables each rule in
turn and asserts the abstain disappears — a guard that passes with itself switched off is
measuring something else.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sb import config, retrieval, subjects   # noqa: E402

fails = []
ASK = "you know any info about sreedhar masula?"


def stops(q):
    retrieval.invalidate()
    r = retrieval.retrieve(q)
    return (bool(r.get("unsupported_terms") or r.get("unknown_subjects")
                 or r.get("subject_miss")), r)


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}{'  ' + detail if detail else ''}")
    if not cond:
        fails.append(label)


print("== with the guard ==")
stopped, r = stops(ASK)
check("abstains", stopped, f"evidence={r['evidence']}")
check("evidence alone would NOT have abstained",
      r["evidence"] >= config.EVIDENCE_STRONG,
      f"{r['evidence']} vs threshold {config.EVIDENCE_STRONG} — "
      f"this is why the score was not enough on its own")

print("\n== with subject awareness removed ==")
real = subjects.unknown_runs
subjects.unknown_runs = lambda *a, **k: []
try:
    stopped_without, r2 = stops(ASK)
finally:
    subjects.unknown_runs = real
    retrieval.invalidate()
check("answers the wrong CV again", not stopped_without,
      f"evidence={r2['evidence']} — reproduces the reported bug")

print("\n== the boost only reorders, it never removes ==")
# The probe has to span two documents even AFTER the distractor filter, or it cannot detect
# eviction and quietly proves nothing.
#
# It used to be "is yaswanth on call this week", chosen because his CV is loud and the rota
# holds the answer. That probe is now single-document by design: the filter drops the rota,
# correctly, because the rota is about Arjun, Priya, Sarah and Meena and not about him —
# which is precisely how the original W31/W32 conflation happened. A good change to the
# product broke the probe rather than the property.
#
# "Roll back in Argo" works instead: Argo is a named system shared by the production and
# staging runbooks, so both survive the filter and the boost has somewhere to reorder.
PROBE = "how do I roll back a release in argo"


def retrieved(boost):
    saved = config.SUBJECT_BOOST
    config.SUBJECT_BOOST = boost
    try:
        retrieval.invalidate()
        return retrieval.retrieve(PROBE)
    finally:
        config.SUBJECT_BOOST = saved
        retrieval.invalidate()


base, noboost = retrieved(config.SUBJECT_BOOST), retrieved(0.0)


def per_wf(r):
    out = {}
    for c in r["chunks"]:
        out[c["wf_key"]] = out.get(c["wf_key"], 0) + 1
    return out


check("the probe needs two documents", len(per_wf(noboost)) > 1,
      f"unboosted={per_wf(noboost)} — a single-document probe cannot detect eviction")
check("the boost retrieves exactly the same chunks",
      {c["chunk_id"] for c in base["chunks"]} == {c["chunk_id"] for c in noboost["chunks"]},
      f"boosted={per_wf(base)} unboosted={per_wf(noboost)}")

print("\n== the distractor filter actually removes the distractor ==")
# The one place subjects filter rather than rank, so it needs the same treatment as the
# guard above: show it doing work, not just show a green result. With subject matching
# disabled the CV should reappear — if it never appeared, the filter is not what is
# keeping it out and this check is measuring nothing.
CROSS = "what is priya's current role"
real_mentions = subjects.mentions
try:
    retrieval.invalidate()
    with_filter = {c["wf_key"] for c in retrieval.retrieve(CROSS)["chunks"]}
    subjects.mentions = lambda *a, **k: set()
    retrieval.invalidate()
    without = {c["wf_key"] for c in retrieval.retrieve(CROSS)["chunks"]}
finally:
    subjects.mentions = real_mentions
    retrieval.invalidate()

check("another person's CV is kept out", "PEOPLE.YASWANTH_CV" not in with_filter,
      str(sorted(with_filter)))
check("and it is the filter doing it", "PEOPLE.YASWANTH_CV" in without,
      f"without subject matching: {sorted(without)}")
check("the document that IS about her survives", "OPS.ONCALL_ROTA" in with_filter,
      str(sorted(with_filter)))

print(f"\n{'PASS' if not fails else 'FAIL'} — {len(fails)} failing")
sys.exit(1 if fails else 0)
