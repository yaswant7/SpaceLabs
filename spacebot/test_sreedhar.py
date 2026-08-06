#!/usr/bin/env python3
"""The reported bug, end to end — and proof the fix is what prevents it.

A guard that passes is worth nothing until you have watched it fail. So this runs the
question twice: once as shipped, and once with subject awareness monkeypatched out, which
should reproduce the original wrong answer. If both runs abstain, the guard is not the
thing doing the work and the test is lying.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sb import rag, retrieval, subjects   # noqa: E402

Q = "you know any info about sreedhar masula?"
fails = []


def run(label):
    retrieval.invalidate()
    r = retrieval.retrieve(Q)
    policy = rag._policy_for(r["evidence"])
    if (r.get("unsupported_terms") or r.get("unknown_subjects")
            or r.get("subject_miss")):
        policy = "nothing"
    print(f"\n--- {label} ---")
    print(f"evidence={r['evidence']}  policy={policy}")
    print(f"unknown_subjects={r.get('unknown_subjects')}  "
          f"subject_miss={r.get('subject_miss')}")
    print("top chunks:")
    for c in r["chunks"][:4]:
        print(f"   {c['score']:.5f} {c['wf_key']:22s} {c['source']:24s} "
              f"about={c.get('subjects')}")
    return policy


shipped = run("as shipped")
if shipped != "nothing":
    fails.append("shipped build should abstain")

real = subjects.unknown_runs
subjects.unknown_runs = lambda *a, **k: []
try:
    broken = run("subject awareness disabled")
finally:
    subjects.unknown_runs = real
    retrieval.invalidate()

if broken == "nothing":
    fails.append("test is vacuous — it abstains even without the guard")
else:
    print(f"\n  (without the guard the pipeline chooses '{broken}' — the original bug)")

print("\n--- the answer users actually get ---")
out = rag.answer(Q, profile="Raj (new hire)")
print(out["answer"])
print(f"\nabstained={out['abstained']}  band={out['band']}  "
      f"sources={[s['wf_key'] for s in out['sources']]}")

low = out["answer"].lower()
for leaked in ["dwp", "power iq", "powerbi", "ctfp", "microsoft store"]:
    if leaked in low:
        fails.append(f"leaked Yaswanth's CV detail: {leaked!r}")
if "sreedhar" not in low:
    fails.append("reply never names who was asked about")
if out["sources"]:
    fails.append("abstaining answer still cites sources")

# The subtler failure: no fact leaks, but the neighbouring topic is offered as though it
# were about the person asked for — "if you're looking for something related to HIS work,
# we cover Yaswanth's CV". That sentence tells the reader Yaswanth's CV is Sreedhar's, which
# is the same conflation in a softer voice.
for phrase in ["his work", "his cv", "his experience", "his background", "his role",
               "his career", "his profile"]:
    if phrase in low:
        fails.append(f"offers another subject as though it were his: {phrase!r}")

# And it must not simply echo the internal note it was handed. Anything in the context that
# reads like a finished sentence is a sentence a small model may repeat rather than rewrite.
for parroted in ["nothing on file is about:", "asked about —", "we hold nothing under",
                 "different subjects we do hold", "nearby topics",
                 "one different thing", "name it exactly"]:
    if parroted in low:
        fails.append(f"parroted the internal note: {parroted!r}")

# Declining is necessary but not sufficient. Every guard added in this session pushes the
# answer toward saying less, and each one passed its own test while the reply quietly got
# less useful — this one lost its "but I do have X" without a single check going red.
# Being unhelpful is a regression too, so it gets an assertion.
#
# Asserted on the METADATA, not the prose. Whether the model works the suggestion into its
# sentence varies run to run, and trying to force it produced worse writing than leaving it
# out did. The offer is now carried as data and rendered as a chip, which is checkable and
# correct every time.
alts = out.get("alternatives") or []
if not any("yaswanth" in (a.get("name") or "").lower() for a in alts):
    fails.append(f"no neighbouring topic offered to the reader: {alts}")

print(f"\n{'PASS' if not fails else 'FAIL'} — {len(fails)} failing")
for f in fails:
    print(f"  - {f}")
sys.exit(1 if fails else 0)
