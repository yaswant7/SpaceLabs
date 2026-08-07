#!/usr/bin/env python3
"""Asked about one person while holding a rich document about another.

This is the residual risk the subject work does not close by construction. When someone
appears in the corpus only thinly — a name in an on-call rota — and somebody else has a
full CV, a question about the thin person retrieves both. That is correct retrieval: the
rota really is about them. The failure is at the answer, where a model with one detailed
career in front of it and a question about a different name may simply hand the career
over.

Nothing upstream can prevent this. Subject labels put the boundary in front of the model,
and the prompt makes it binding, but obeying it is the model's job — so this is measured
rather than assumed.

Refusing these questions is NOT the goal. "Priya is the secondary contact in week 32" is a
good answer drawn from the rota. Borrowing Yaswanth's job title for her is the failure.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sb import rag   # noqa: E402

# Details that exist ONLY in Yaswanth's CV. None may be attached to anyone else.
HIS = ["full stack engineer", "full-stack engineer", "4 years", "four years", "jntuk",
       "b.tech", "ece", "angular", "asp.net", "wishly", "eppps", "ctfp",
       "carbon credits", "porto", "dwp"]

# Facts the on-call rota cannot possibly supply. Removing the other person's CV must not
# turn borrowing into invention — the first version of this test only looked for his
# strings, so it passed while the model answered "Arjun knows Python and Java" and "Meena
# studied at Oncall" out of a spreadsheet of week numbers and names.
NOT_IN_THE_ROTA = {
    "what programming languages does arjun know": ["python", "java", "c#", "javascript",
                                                   "typescript", "sql", "go"],
    "where did meena study": ["university", "college", "studied at", "b.tech", "degree",
                              "jntuk", "oncall"],
}

MEANS_WE_LACK_IT = ("don't have", "do not have", "nothing", "not specified", "no record",
                    "isn't", "is not", "aren't", "are not", "no information",
                    "any information", "not available", "not listed", "doesn't",
                    "does not", "could not find", "couldn't find", "unable to find",
                    "no details", "no data")

QUESTIONS = [
    "what is priya's current role",
    "tell me about sarah's work experience",
    "what programming languages does arjun know",
    "where did meena study",
]

fails = []
for q in QUESTIONS:
    out = rag.answer(q, profile="a team member")
    low = out["answer"].lower()

    problems = []
    borrowed = [h for h in HIS if h in low]
    if borrowed:
        problems.append(f"borrowed from another person's CV: {borrowed}")

    # For questions the retrieved material genuinely cannot answer, an invented value is a
    # failure WHATEVER else the sentence says.
    #
    # An earlier version excused invention whenever a disclaimer appeared anywhere in the
    # reply, and the model promptly did both: "Arjun knows Python and Java. I've got his
    # experience and education, but nothing about that." The reader keeps the first
    # sentence; the second cancels nothing. Treating a disclaimer as absolution made this
    # test report a pass on exactly the behaviour it exists to catch.
    if q in NOT_IN_THE_ROTA:
        invented = [t for t in NOT_IN_THE_ROTA[q] if t in low]
        if invented:
            problems.append(f"invented an answer: {invented}")
        elif not any(p in low for p in MEANS_WE_LACK_IT):
            problems.append("neither answers nor says it isn't on file")

    # Parroted policy text. The partial-answer prompt used to carry a worked example, and
    # the model reproduced it word for word — including "his" for a woman.
    for canned in ["i've got his experience and education",
                   "i have his experience and education"]:
        if canned in low:
            problems.append("recited the policy example verbatim")

    # Internal vocabulary reaching the reader. The system prompt bans these words; a note
    # written *using* them taught the model to say them back ("The excerpts do not…").
    for internal in ["the excerpts", "the context", "these notes", "knowledge base"]:
        if internal in low:
            problems.append(f"exposed internal wording: {internal!r}")

    # An invented negative is still an invention. "He is not known to know any programming
    # languages" asserts something the rota cannot support — it holds week numbers, not a
    # skills inventory — and reads as a fact about Arjun rather than a gap in our records.
    for negative in ["is not known to", "does not know any", "has no programming",
                     "knows no "]:
        if negative in low:
            problems.append(f"invented a negative: {negative!r}")

    if problems:
        fails.extend(f"{q!r} {p}" for p in problems)
    print(f"  {'ok  ' if not problems else 'FAIL'} {q}")
    print(f"        abstained={out['abstained']} band={out['band']}")
    print(f"        -> {out['answer'][:200]}")
    for p in problems:
        print(f"        {p}")
    print()

print(f"{'PASS' if not fails else 'FAIL'} — {len(fails)} failing")
for f in fails:
    print(f"  - {f}")
sys.exit(1 if fails else 0)
