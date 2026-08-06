#!/usr/bin/env python3
"""The second reported conflation: "in what timings is he available?" answered by
attributing the on-call rota to the person in the CV. He is not in the rota."""
import http.cookiejar
import json
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
    text, meta, cid, ev = "", None, conv, None
    for raw in r:
        line = raw.decode("utf-8", "replace").rstrip("\n")
        if line.startswith("event:"):
            ev = line[6:].strip()
        elif line.startswith("data:"):
            try:
                d = json.loads(line[5:].strip())
            except ValueError:
                continue
            if ev == "delta":
                text += d
            elif ev == "meta":
                meta = d
            elif ev == "conversation":
                cid = d["id"]
    return text.strip(), meta or {}, cid


fails = 0
print("Turn 1 — ask about the person")
t1, m1, cid = ask("may i know complete profile of Yaswanth")
print(f"  -> {t1[:90]}...")

print("\nTurn 2 — the trap: an availability question about that person")
t2, m2, _ = ask("in what timings is he available?", cid)
print(f"  -> {t2[:260]}")
wfs = (m2.get("retrieval") or {}).get("workflows") or []
print(f"  grounded in: {wfs}  band={m2.get('band')} conf={m2.get('confidence')}")

low = t2.lower()
checks = [
    ("does not put him on the on-call rota",
     not any(w in low for w in ("on-call schedule is", "primary contact for week",
                                "w32", "w31", "w33"))),
    ("does not name unrelated rota people",
     not any(n in low for n in ("arjun", "priya", "meena", "sarah"))),
    # Phrase list, not a single form, because the model varies its wording run to run and
    # this check is about MEANING. It previously held "is not" but not "are not", so
    # "Yaswanth's availability timings are not available on file" — a perfectly good answer
    # — was recorded as a failure.
    #
    # Widening this cannot make the check vacuous: the failure it exists to catch is the
    # model answering from the conversation instead, replaying turn 1's "Yaswanth Kamineni
    # is a Full Stack Engineer with 4 years of experience…", which matches none of these.
    ("says plainly it isn't on file",
     any(p in low for p in ("don't have", "do not have", "not specified", "nothing about",
                            "isn't", "is not", "aren't", "are not", "no information",
                            "not available", "no record", "nothing on file"))),
]
for label, ok in checks:
    print(f"  {'ok  ' if ok else 'FAIL'} {label}")
    fails += 0 if ok else 1

print("\nTurn 3 — the rota itself must still answer correctly")
t3, m3, _ = ask("who is the primary on-call for week 2026-W32?")
print(f"  -> {t3[:130]}")
ok = "arjun" in t3.lower()
print(f"  {'ok  ' if ok else 'FAIL'} rota still answers (expects arjun)")
fails += 0 if ok else 1

print(f"\n{4 - fails}/4 checks passed")
sys.exit(1 if fails else 0)
