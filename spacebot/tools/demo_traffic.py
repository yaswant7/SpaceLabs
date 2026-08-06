#!/usr/bin/env python3
"""Ask a handful of realistic questions so the admin overview has something to show.

An empty dashboard demos badly — "most used knowledge" with no rows says nothing about what
the feature does. This asks as the new hire, through the real API, so the usage counts and
the who-asked-what trail are genuine rather than fabricated rows in a table.
"""
import http.client
import json
import sys

HOST, PORT = "127.0.0.1", 8080

QUESTIONS = [
    "how do I roll back a production deploy",
    "how do I set up my local dev environment",
    "how do I request aws access",
    "who is on call in week 32",
    "what is the hotel limit on expenses",
    "how do I connect to the vpn",
    "which vendors has meena approved",
    "how do I roll back a production deploy",
    "what do I need before my first on-call shift",
    "how do I create a purchase order",
    "what is the office wifi password",
    "how much annual leave do I get",
]


def login():
    c = http.client.HTTPConnection(HOST, PORT, timeout=60)
    c.request("POST", "/api/login",
              json.dumps({"email": "yaswanth@spacelabs.dev",
                          "password": "yaswanth123"}).encode(),
              {"content-type": "application/json"})
    r = c.getresponse()
    r.read()
    cookie = r.getheader("Set-Cookie", "").split(";")[0]
    c.close()
    return cookie


cookie = login()
if not cookie:
    print("could not sign in — is the server running?")
    sys.exit(1)

for i, q in enumerate(QUESTIONS, 1):
    c = http.client.HTTPConnection(HOST, PORT, timeout=600)
    c.request("POST", "/api/ask", json.dumps({"question": q}).encode(),
              {"Cookie": cookie, "content-type": "application/json"})
    r = c.getresponse()
    out = json.loads(r.read() or b"{}")
    c.close()
    mark = "—" if out.get("abstained") else "✓"
    print(f"  [{i}/{len(QUESTIONS)}] {mark} {q}")

print("\ndone — the admin Overview now has usage to show")
