#!/usr/bin/env python3
"""Regression for the conflation bug: asking about a PERSON mid-conversation about
VENDORS must not answer with vendor facts about that person.

Drives the real HTTP path, including conversation memory, so the condenser runs for real.
"""
import http.cookiejar
import json
import re
import sys
import urllib.request

BASE = "http://127.0.0.1:8080"
jar = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
op.open(urllib.request.Request(
    BASE + "/api/login",
    data=json.dumps({"email": "yaswanth@spacelabs.dev", "password": "yaswanth123"}).encode(),
    headers={"content-type": "application/json"})).read()


def ask(q, conv=None):
    payload = {"question": q}
    if conv:
        payload["conversation_id"] = conv
    r = op.open(urllib.request.Request(
        BASE + "/api/ask/stream", data=json.dumps(payload).encode(),
        headers={"content-type": "application/json"}), timeout=600)
    text, meta, cid, event = "", None, conv, None
    for raw in r:
        line = raw.decode("utf-8", "replace").rstrip("\n")
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            try:
                d = json.loads(line[5:].strip())
            except ValueError:
                continue
            if event == "delta":
                text += d
            elif event == "meta":
                meta = d
            elif event == "conversation":
                cid = d["id"]
    return text.strip(), meta or {}, cid


fails = 0
print("Turn 1 — establish a vendor/purchase-order conversation")
t1, m1, cid = ask("what do I need before I can raise a purchase order for a brand new supplier?")
print(f"  -> {t1[:100]}...")
print(f"  grounded in: {(m1.get('retrieval') or {}).get('workflows')}")

print("\nTurn 2 — the trap: switch subject to a person")
t2, m2, _ = ask("you know anything about yaswanth ?", cid)
print(f"  -> {t2[:220]}")
wfs = (m2.get("retrieval") or {}).get("workflows") or []
print(f"  grounded in: {wfs}   band={m2.get('band')} conf={m2.get('confidence')}")

low = t2.lower()
checks = [
    ("retrieved the CV, not vendor docs", "PEOPLE.YASWANTH_CV" in wfs),
    ("does not call him a vendor", "vendor" not in low),
    ("does not claim pending approval", "pending" not in low),
    ("actually says something about him",
     any(w in low for w in ("engineer", "developer", "experience", "jntuk", "full stack"))),
]
for label, ok in checks:
    print(f"  {'ok  ' if ok else 'FAIL'} {label}")
    fails += 0 if ok else 1

print("\nTurn 3 — a genuine follow-up must still resolve against the conversation")
t3, m3, _ = ask("where did he study?", cid)
print(f"  -> {t3[:160]}")
ok = "jntuk" in t3.lower()
print(f"  {'ok  ' if ok else 'FAIL'} pronoun resolved to Yaswanth (expects JNTUK)")
fails += 0 if ok else 1

print("\nTurn 4 — and the vendor thread still works in the same conversation")
t4, m4, _ = ask("is Globex approved?", cid)
print(f"  -> {t4[:160]}")
ok = "pending" in t4.lower() or "not" in t4.lower()
print(f"  {'ok  ' if ok else 'FAIL'} Globex correctly reported as not approved")
fails += 0 if ok else 1

print(f"\n{7 - fails}/7 checks passed")
sys.exit(1 if fails else 0)
