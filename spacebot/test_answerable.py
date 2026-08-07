#!/usr/bin/env python3
"""Questions a CV plainly answers must be answered, whatever words they use.

Every guard in this system pushes toward saying less, and the failure they create is silent:
nothing errors, no test goes red, the assistant just stops being useful. This suite is the
counterweight — the questions a reader would call obviously answerable, phrased the way
people actually phrase them rather than the way the document is written.

Taken from a real session where the assistant refused all of them:
    "may i know complete profile of Yaswanth"          -> "We don't have any information"
    "what are all the technologies that yaswanth ..."  -> "We don't have any information"

Both were refused out of a CV that lists his role, his stack, his projects and his degree.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sb import rag   # noqa: E402

fails = []

# Question, and words that prove the answer actually came from his CV.
CASES = [
    ("may i know complete profile of Yaswanth",
     ["engineer", "developer", "experience", "stack", "angular", "python", "jntuk"]),
    ("what are all the technologies that yaswanth can able to do",
     ["angular", "asp", "python", "java", "sql", "net", "stack"]),
    ("give me an overview of yaswanth",
     ["engineer", "developer", "experience", "stack", "project"]),
    ("what is yaswanth's background",
     ["engineer", "developer", "experience", "education", "jntuk", "project"]),
    ("what tech stack does yaswanth work with",
     ["angular", "asp", "python", "java", "sql", "net"]),
    ("tell me about yaswanth's education",
     ["jntuk", "b.tech", "bachelor", "ece", "university"]),
    ("what is yaswanth's current role",
     ["engineer", "developer", "full stack"]),
    ("which vendors has meena approved",
     ["acme", "globex", "initech", "approved"]),
]

def confident(out):
    """A broad question about a document we hold should read as confident.

    "Anything about Yaswanth" answered in full at high confidence; "may I know the complete
    profile of Yaswanth" came back hedged and stamped low confidence — the same question,
    differing only in which filler words the person typed. A correct answer delivered as if
    we half-believe it is still a defect, so the band is asserted, not just the content.
    """
    return out.get("band") in ("high", "medium")


BROAD = {
    "may i know complete profile of Yaswanth",
    "give me an overview of yaswanth",
    "what is yaswanth's background",
}

for question, evidence in CASES:
    out = rag.answer(question, profile="Yaswanth (new hire)")
    low = out["answer"].lower()
    grounded = [w for w in evidence if w in low]

    if question in BROAD and not out["abstained"] and not confident(out):
        fails.append(f"{question!r} answered but stamped {out.get('band')} confidence")

    if out["abstained"]:
        fails.append(f"{question!r} was refused")
        verdict, detail = "FAIL", "REFUSED — this is answerable from the CV"
    elif not grounded:
        fails.append(f"{question!r} answered without anything from the document")
        verdict, detail = "FAIL", "answered, but nothing from the source appears"
    else:
        verdict, detail = "ok  ", f"grounded in {grounded[:3]}"

    print(f"  {verdict} {question}")
    print(f"        {detail}")
    print(f"        -> {out['answer'][:150].replace(chr(10), ' ')}")
    print()

print(f"{'PASS' if not fails else 'FAIL'} — {len(fails)} failing")
for f in fails:
    print(f"  - {f}")
sys.exit(1 if fails else 0)
