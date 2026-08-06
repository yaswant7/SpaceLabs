#!/usr/bin/env python3
"""A question whose words are all in the corpus, but scattered, is not a question we can
answer.

The failure this exists for: "how to read github secrets of a project I've access" came
back with the Corporate VPN and AWS access steps, at 0.549 confidence. `github` really was
in the corpus — in a CV. `access` matched two runbooks. Nothing was about GitHub secrets,
but every retriever found something and agreed, which is what the evidence score rewards.

None of the other guards can see this: `_unsupported_terms` needs every word missing,
`unknown_runs` needs an unrecognised name, `_attribute_miss` needs a known subject. So this
checks the thing they all skip — whether one single document actually covers the question.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sb import config, rag, retrieval   # noqa: E402

fails = []


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}{'  ' + detail if detail else ''}")
    if not cond:
        fails.append(label)


def stops(q):
    r = retrieval.retrieve(q)
    return bool(r.get("unsupported_terms") or r.get("unknown_subjects")
                or r.get("subject_miss") or r.get("thin_match")), r


print("== scattered matches must not become answers ==")
SCATTERED = [
    "how to read github secrets of a project ive access",
    "how do I add a user to the github org",
    "how do I claim overtime for a weekend release",
    "where do I file a travel visa request",
    "how do I rotate the database password",
    "how do I book a meeting room",
]
for q in SCATTERED:
    stopped, r = stops(q)
    check(q, stopped, f"cov={r.get('coverage')} ev={r['evidence']} thin={r.get('thin_match')}")

print("\n== and real questions still answer ==")
WORKING = [
    "how do I roll back a production deploy",
    "how do I set up my local dev environment",
    "how do I request aws access",
    "who is on call in week 32",
    "what is the hotel limit on expenses",
    "how do I connect to the vpn",
    "which vendors has meena approved",
    "what do I need before my first on-call shift",
    "how do I create a purchase order",
    "what is yaswanth's current role",
    "when was modern signal founded",
    "what programming languages does yaswanth know",
]
for q in WORKING:
    stopped, r = stops(q)
    check(q, not stopped, f"cov={r.get('coverage')} ev={r['evidence']}")

print("\n== the threshold sits in a measured gap, not on an edge ==")
covs_bad = [retrieval.retrieve(q).get("coverage") for q in SCATTERED]
covs_ok = [retrieval.retrieve(q).get("coverage") for q in WORKING]
check("no overlap between the two groups", max(covs_bad) < min(covs_ok),
      f"bad≤{max(covs_bad)} < ok≥{min(covs_ok)}")
check("threshold is between them",
      max(covs_bad) < config.MIN_QUERY_COVERAGE <= min(covs_ok),
      f"{max(covs_bad)} < {config.MIN_QUERY_COVERAGE} <= {min(covs_ok)}")

print("\n== and the guard is what does it ==")
saved = config.MIN_QUERY_COVERAGE
config.MIN_QUERY_COVERAGE = 0.0
try:
    without, r = stops("how to read github secrets of a project ive access")
finally:
    config.MIN_QUERY_COVERAGE = saved
check("without the guard it answers again", not without,
      f"ev={r['evidence']} — reproduces the reported bug")

print("\n== end to end ==")
out = rag.answer("how to read github secrets of a project ive access",
                 profile="Yaswanth (new hire)")
print(f"  -> {out['answer'][:200]}")
low = out["answer"].lower()
check("abstains", out["abstained"], f"band={out['band']}")
for leaked in ["tailscale", "argo", "iam", "access portal", "spacectl", "pending approval"]:
    if leaked in low:
        fails.append(f"leaked unrelated runbook detail: {leaked!r}")
check("no unrelated runbook steps", not any(
    x in low for x in ["tailscale", "argo", "iam", "access portal", "spacectl"]))

print(f"\n{'PASS' if not fails else 'FAIL'} — {len(fails)} failing")
for f in fails:
    print(f"  - {f}")
sys.exit(1 if fails else 0)
