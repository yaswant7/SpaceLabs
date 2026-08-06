#!/usr/bin/env python3
"""Print the admin overview as the API returns it — what the dashboard renders."""
import http.client
import json

HOST, PORT = "127.0.0.1", 8080

c = http.client.HTTPConnection(HOST, PORT, timeout=60)
c.request("POST", "/api/login",
          json.dumps({"email": "admin@spacelabs.dev", "password": "admin123"}).encode(),
          {"content-type": "application/json"})
r = c.getresponse(); r.read()
cookie = r.getheader("Set-Cookie", "").split(";")[0]
c.request("GET", "/api/admin/overview", headers={"Cookie": cookie})
o = json.loads(c.getresponse().read())
c.close()

print(f"published {o['published']}/{o['workflows']} · {o['asks']} asks in {o['days']}d · "
      f"{o['answer_rate']}% answered · {o['abstained']} with nothing on file · "
      f"{o['gaps_open']} open gaps · {o['people']} people\n")

print("most used knowledge")
for a in o["top_artifacts"]:
    print(f"   {a['uses']:>3}  {a['name']}")

print("\npublished, never used")
for u in o["unused"] or [{"name": "(none)"}]:
    print(f"        {u['name']}")

print("\nknowledge changes")
for a in o["recent_activity"][:8]:
    print(f"   {a['actor'] or '—':<20} {a['action']:<10} {a['wf_key'] or '—':<24} {a['detail']}")

print("\nlatest questions")
for q in o["recent_questions"][:8]:
    mark = "nothing on file" if q["abstained"] else "answered"
    print(f"   {q['asked_by'] or '—':<20} {mark:<16} {q['question'][:52]}")
