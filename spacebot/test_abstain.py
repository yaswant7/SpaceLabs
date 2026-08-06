"""The abstain path must be accurate, not just non-committal.

Every reply here is one we cannot ground, which makes it the easiest place to invent: a
topic that sounds like it ought to exist ("our Employee Handbook"), an owner for a
company-wide policy, or the reader's own name mistaken for the subject of their question.
All three were observed while tuning this prompt. Two runs per question, because the
failures are intermittent.
"""
import sys
sys.path.insert(0, ".")
from sb import db, rag

REAL = {c["name"].lower() for c in db.get_catalog()}
INVENTED = ["employee handbook", "cv templates", "hr portal", "intranet", "wiki",
            "onboarding handbook", "staff directory"]

QS = ["you know any info about sreedhar masula?",
      "what's the office wifi password?",
      "how much annual leave do I get?",
      "where do I park my bike?"]

bad = 0
for q in QS:
    for _ in range(2):
        out = rag.answer(q, profile="Raj (new hire)")
        low = out["answer"].lower()
        flags = [f"INVENTED:{t}" for t in INVENTED if t in low]
        if "raj" in low:
            flags.append("TREATS-READER-AS-SUBJECT")
        # A stray title on its own line, not merely a line break. Two sentences in two
        # paragraphs read perfectly well; "Roll back a production deploy." sitting alone
        # under the refusal does not, and neither does "Expense Policy:" used as a label.
        # Flagging every newline condemned the good shape along with the bad one.
        for line in (l.strip() for l in out["answer"].splitlines()):
            if line and len(line.split()) < 6:
                flags.append(f"FRAGMENT:{line[:40]!r}")
            if line.endswith(":"):
                flags.append(f"USED-A-LABEL:{line[:40]!r}")
        bad += len(flags)
        print(f"Q: {q}")
        print(f"   {'  '.join(flags) if flags else 'clean'}")
        print(f"   -> {out['answer']}")
    print()

print(f"{'PASS' if not bad else 'FAIL'} — {bad} problems")
sys.exit(1 if bad else 0)
