#!/usr/bin/env python3
"""Conversations should name themselves, and a person should be able to overrule that.

Two properties, and the second is the one that protects the user: a model-written title is
a convenience, but a title someone typed is a decision, and nothing may overwrite it.

Runs against the live server so it exercises the real SSE path — the title arrives as an
event after the answer, which is the part that could silently never fire.
"""
import http.client
import json
import sys

HOST, PORT = "127.0.0.1", 8080
fails = []


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}{'  ' + detail if detail else ''}")
    if not cond:
        fails.append(label)


def login():
    c = http.client.HTTPConnection(HOST, PORT, timeout=60)
    c.request("POST", "/api/login",
              json.dumps({"email": "yaswanth@spacelabs.dev",
                          "password": "yaswanth123"}).encode(),
              {"content-type": "application/json"})
    r = c.getresponse(); r.read()
    cookie = r.getheader("Set-Cookie", "").split(";")[0]
    c.close()
    return cookie


def ask(cookie, question, conv=None):
    """Returns (conversation_id, provisional_title, model_title_or_None)."""
    c = http.client.HTTPConnection(HOST, PORT, timeout=900)
    body = {"question": question}
    if conv:
        body["conversation_id"] = conv
    c.request("POST", "/api/ask/stream", json.dumps(body).encode(),
              {"Cookie": cookie, "content-type": "application/json"})
    r = c.getresponse()
    cid = provisional = model_title = None
    event = None
    for raw in r.read().decode("utf-8", "replace").splitlines():
        if raw.startswith("event:"):
            event = raw[6:].strip()
        elif raw.startswith("data:"):
            payload = raw[5:].strip()
            if event == "conversation":
                d = json.loads(payload)
                cid, provisional = d["id"], d["title"]
            elif event == "title":
                model_title = json.loads(payload)["title"]
    c.close()
    return cid, provisional, model_title


def get_title(cookie, cid):
    c = http.client.HTTPConnection(HOST, PORT, timeout=60)
    c.request("GET", f"/api/conversations", headers={"Cookie": cookie})
    convs = json.loads(c.getresponse().read())
    c.close()
    return next((x["title"] for x in convs if x["id"] == cid), None)


def rename(cookie, cid, title):
    c = http.client.HTTPConnection(HOST, PORT, timeout=60)
    c.request("POST", f"/api/conversations/{cid}/rename",
              json.dumps({"title": title}).encode(),
              {"Cookie": cookie, "content-type": "application/json"})
    st = c.getresponse().status
    c.close()
    return st


cookie = login()
check("signed in", bool(cookie))

print("\n== a new conversation names itself ==")
Q = "what do I need before my first on-call shift"
cid, provisional, model_title = ask(cookie, Q)
print(f"  provisional : {provisional!r}")
print(f"  model title : {model_title!r}")
check("a title arrives before the answer", bool(provisional))
check("the model sends a better one afterwards", bool(model_title))
if model_title:
    check("it isn't just the question truncated", model_title != provisional)
    check("it is short enough for a sidebar", len(model_title) <= 60, f"{len(model_title)} chars")
    check("no trailing full stop", not model_title.endswith("."))
    check("not wrapped in quotes", not (model_title[0] in "\"'" or model_title[-1] in "\"'"))
    check("it is what got saved", get_title(cookie, cid) == model_title,
          repr(get_title(cookie, cid)))

print("\n== a second message does not rename it ==")
before = get_title(cookie, cid)
_, _, second = ask(cookie, "and what about the escalation path", conv=cid)
check("no title event on a follow-up", second is None, repr(second))
check("the name is unchanged", get_title(cookie, cid) == before, repr(get_title(cookie, cid)))

print("\n== a person can overrule the model ==")
st = rename(cookie, cid, "My on-call notes")
check("rename accepted", st == 200, f"HTTP {st}")
check("the new name stuck", get_title(cookie, cid) == "My on-call notes",
      repr(get_title(cookie, cid)))

print("\n== and their choice survives the next message ==")
_, _, t3 = ask(cookie, "who do I call if the pager fails", conv=cid)
check("no title event", t3 is None)
check("their name is still theirs", get_title(cookie, cid) == "My on-call notes",
      repr(get_title(cookie, cid)))

print(f"\n{'PASS' if not fails else 'FAIL'} — {len(fails)} failing")
for f in fails:
    print(f"  - {f}")
sys.exit(1 if fails else 0)
