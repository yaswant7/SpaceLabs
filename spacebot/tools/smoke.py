#!/usr/bin/env python3
"""Is the app actually usable right now? Every layer, one request each.

Not a substitute for the test suite — this answers "is it up and working", which is what
you want to know in ten seconds before a demo, not "is it correct".
"""
import http.client
import json
import sys

HOST = "127.0.0.1"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
bad = 0


def show(label, ok, detail=""):
    global bad
    bad += 0 if ok else 1
    print(f"  {'ok  ' if ok else 'FAIL'} {label}{'  ' + detail if detail else ''}")


def req(method, path, cookie=None, body=None, timeout=600):
    c = http.client.HTTPConnection(HOST, PORT, timeout=timeout)
    h = {}
    if cookie:
        h["Cookie"] = cookie
    if body is not None:
        h["content-type"] = "application/json"
    c.request(method, path, json.dumps(body).encode() if body is not None else None, h)
    r = c.getresponse()
    data = r.read()
    st, hdr = r.status, r.getheader("Set-Cookie", "")
    c.close()
    return st, data, hdr


print(f"Spacebot smoke test on :{PORT}\n")

st, _, _ = req("GET", "/login")
show("login page serves", st == 200, f"HTTP {st}")

for asset in ("app.js", "app.css", "icons.js", "markdown.js"):
    st, data, _ = req("GET", f"/static/{asset}")
    show(f"static/{asset}", st == 200 and len(data) > 100, f"HTTP {st}, {len(data)}B")

st, data, cookie_hdr = req("POST", "/api/login",
                           body={"email": "yaswanth@spacelabs.dev",
                                 "password": "yaswanth123"})
cookie = cookie_hdr.split(";")[0] if cookie_hdr else ""
show("sign in as the new hire", st == 200 and bool(cookie), f"HTTP {st}")

st, data, _ = req("GET", "/api/me", cookie)
me = json.loads(data or b"{}")
show("session works", st == 200 and me.get("name"), me.get("name", ""))
show("model provider resolved", me.get("provider") not in (None, "mock"),
     str(me.get("provider")))

st, data, _ = req("GET", "/api/index/status", cookie)
show("index status is author-only", st == 403, f"HTTP {st}")

print("\n  asking a real question (a local model on CPU takes a minute)…")
st, data, _ = req("POST", "/api/ask", cookie,
                  {"question": "how do I roll back a production deploy"})
out = json.loads(data or b"{}")
show("answer returned", st == 200 and bool(out.get("answer")), f"HTTP {st}")
show("grounded in a workflow", bool(out.get("sources")),
     ", ".join(s["wf_key"] for s in out.get("sources", [])))
show("did not abstain", out.get("abstained") is False, f"band={out.get('band')}")
print(f"       -> {(out.get('answer') or '')[:120]}…")

print("\n  asking something we do not document…")
st, data, _ = req("POST", "/api/ask", cookie, {"question": "what is the office wifi password"})
out2 = json.loads(data or b"{}")
show("declines instead of inventing", out2.get("abstained") is True,
     f"band={out2.get('band')}")
print(f"       -> {(out2.get('answer') or '')[:120]}")

print(f"\n{'ALL GOOD' if not bad else str(bad) + ' PROBLEM(S)'}")
sys.exit(1 if bad else 0)
