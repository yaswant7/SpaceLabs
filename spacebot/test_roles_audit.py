#!/usr/bin/env python3
"""Who can do what, and does the audit trail actually record it?

Runs against the live server, because roles are enforced in the HTTP layer and testing the
functions underneath would prove nothing about whether the routes are gated.

The negative cases matter more than the positive ones. Any demo will show that a senior can
edit a document; nobody notices that a new hire can too until it is somebody's production
knowledge base.
"""
import http.client
import os
import json
import sys

HOST, PORT = "127.0.0.1", 8080
fails = []

USERS = {
    "yaswanth": ("yaswanth@spacelabs.dev", "yaswanth123"),
    "roshan": ("roshan@spacelabs.dev", "roshan123"),
    "admin": ("admin@spacelabs.dev", "admin123"),
}


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}{'  ' + detail if detail else ''}")
    if not cond:
        fails.append(label)


def login(who):
    email, pw = USERS[who]
    c = http.client.HTTPConnection(HOST, PORT, timeout=60)
    c.request("POST", "/api/login", json.dumps({"email": email, "password": pw}).encode(),
              {"content-type": "application/json"})
    r = c.getresponse()
    r.read()
    cookie = r.getheader("Set-Cookie", "").split(";")[0]
    c.close()
    return cookie


def call(method, path, cookie, body=None):
    c = http.client.HTTPConnection(HOST, PORT, timeout=120)
    headers = {"Cookie": cookie, "content-type": "application/json"}
    c.request(method, path, json.dumps(body or {}).encode(), headers)
    r = c.getresponse()
    raw = r.read()
    c.close()
    try:
        return r.status, json.loads(raw or b"{}")
    except ValueError:
        return r.status, {}


sess = {who: login(who) for who in USERS}
print("signed in:", ", ".join(f"{w}={'yes' if sess[w] else 'NO'}" for w in sess))
for who, cookie in sess.items():
    check(f"{who} can sign in", bool(cookie))

print("\n== a new hire gets chat and nothing else ==")
st, me = call("GET", "/api/me", sess["yaswanth"])
check("identified correctly", me.get("name", "").startswith("Yaswanth"), me.get("name", ""))
check("not an author", me.get("can_author") is False)
check("not an admin", me.get("is_admin") is False)
for path in ("/api/catalog", "/api/gaps"):
    st, _ = call("GET", path, sess["yaswanth"])
    check(f"blocked from {path}", st == 403, f"HTTP {st}")
st, _ = call("GET", "/api/admin/overview", sess["yaswanth"])
check("blocked from the audit overview", st == 403, f"HTTP {st}")

print("\n== a senior can see and change published knowledge ==")
st, me = call("GET", "/api/me", sess["roshan"])
check("identified correctly", me.get("name", "").startswith("Roshan"), me.get("name", ""))
check("is an author", me.get("can_author") is True)
check("is not an admin", me.get("is_admin") is False)

st, cat = call("GET", "/api/catalog", sess["roshan"])
check("can list the catalog", st == 200 and isinstance(cat, list), f"HTTP {st}")
published = [w for w in cat if w.get("status") == "published"]
check("published entries are visible", bool(published), f"{len(published)} published")

key = published[0]["wf_key"] if published else None
if key:
    st, w = call("GET", f"/api/workflows/{key}", sess["roshan"])
    check("can view one in full", st == 200 and w.get("wf_key") == key, f"HTTP {st}")

    original = w.get("category", "")
    st, _ = call("POST", f"/api/workflows/{key}/update", sess["roshan"],
                 {"category": "Edited By Test"})
    check("can edit it", st == 200, f"HTTP {st}")
    st, again = call("GET", f"/api/workflows/{key}", sess["roshan"])
    check("the edit stuck", again.get("category") == "Edited By Test",
          again.get("category", ""))
    check("and it is attributed", (again.get("updated_by") or "").startswith("Roshan"),
          again.get("updated_by", ""))
    call("POST", f"/api/workflows/{key}/update", sess["roshan"], {"category": original})

st, _ = call("GET", "/api/admin/overview", sess["roshan"])
check("but not the audit overview", st == 403, f"HTTP {st}")

print("\n== a new hire cannot edit or delete ==")
if key:
    st, _ = call("POST", f"/api/workflows/{key}/update", sess["yaswanth"], {"name": "hijacked"})
    check("blocked from editing", st == 403, f"HTTP {st}")
    st, _ = call("POST", f"/api/workflows/{key}/delete", sess["yaswanth"])
    check("blocked from deleting", st == 403, f"HTTP {st}")
    st, still = call("GET", f"/api/workflows/{key}", sess["roshan"])
    check("the entry is untouched", still.get("name") != "hijacked", still.get("name", ""))

print("\n== delete really removes it ==")
st, _ = call("POST", "/api/workflows/TEST.DELETE_ME/delete", sess["roshan"])
check("deleting something absent 404s", st == 404, f"HTTP {st}")

# The round trip that matters. A delete that leaves chunks behind means the document keeps
# answering questions after somebody decided it was wrong — the worst outcome available,
# and invisible from the catalog, which would show it gone.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sb import chunks as chunkstore, db as sdb   # noqa: E402

TMP = "TEST.AUDIT_ROUNDTRIP"
sdb.upsert_workflow({
    "wf_key": TMP, "name": "Feed the office axolotl", "summary":
        "How to feed the office axolotl, a task nothing else in this corpus mentions.",
    "category": "Test", "owner": "tester", "status": "published", "actor": "Test Harness",
    "steps": [{"title": "Thaw the bloodworm", "body": "Take one cube from the freezer.",
               "verification": "The cube is soft."}],
    "known_errors": [], "faqs": [], "trigger_phrases": ["feed the axolotl"],
})
card = next((c for c in sdb.get_catalog() if c["wf_key"] == TMP), None)
if card:
    chunkstore.reindex_workflow(card["id"])
    chunkstore.embed_pending()
before = len([c for c in chunkstore.all_chunks() if c["wf_key"] == TMP])
check("the throwaway entry is indexed", before > 0, f"{before} chunks")

st, _ = call("POST", f"/api/workflows/{TMP}/delete", sess["roshan"])
check("a senior can delete it", st == 200, f"HTTP {st}")
after = len([c for c in chunkstore.all_chunks() if c["wf_key"] == TMP])
check("its chunks are gone too", after == 0, f"{after} left")
st, cat2 = call("GET", "/api/catalog", sess["roshan"])
check("it is out of the catalog", not any(w["wf_key"] == TMP for w in cat2))

print("\n== the admin sees what happened ==")
st, o = call("GET", "/api/admin/overview", sess["admin"])
check("overview loads", st == 200, f"HTTP {st}")
check("counts published knowledge", isinstance(o.get("published"), int), str(o.get("published")))
check("reports the answer rate", "answer_rate" in o)
acts = o.get("recent_activity") or []
check("the senior's edit is in the trail", any(
    a.get("action") == "edited" and (a.get("actor") or "").startswith("Roshan") for a in acts),
    f"{len(acts)} entries")
check("the trail says which entry", any(a.get("wf_key") for a in acts))
check("most-used knowledge is reported", isinstance(o.get("top_artifacts"), list),
      f"{len(o.get('top_artifacts') or [])} ranked")
check("recent questions are reported", isinstance(o.get("recent_questions"), list))

print(f"\n{'PASS' if not fails else 'FAIL'} — {len(fails)} failing")
for f in fails:
    print(f"  - {f}")
sys.exit(1 if fails else 0)
