"""Spacebot web server — pure stdlib, no framework.

    python3 seed.py && python3 server.py      # http://localhost:8080

Three experiences behind one login:
  - End user  -> a clean "ask me anything" chat. Never sees the workflow catalog.
  - Author    -> Knowledge Studio: add knowledge, catalog, and the demand-ranked gaps.
  - Admin     -> also Model settings (local Ollama, or bring your own hosted key).

This file is deliberately thin. It owns HTTP concerns only — routing, auth, serialisation,
SSE framing — and delegates everything else: retrieval and answering to sb.rag, model
choice to sb.providers, storage to sb.db, and the entire UI to sb/web/. Adding an
endpoint is one entry in ROUTES; changing the UI never touches Python.
"""
import errno
import json
import mimetypes
import os
import re
import subprocess
from functools import wraps
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from sb import auth, chunks, config, db, ingest, settings
from sb.media import blob as mblob
from sb.media import pipeline as mpipe
from sb.rag import answer, answer_stream

PORT = int(os.environ.get("SPACEBOT_PORT", "8080"))
WEB_DIR = os.path.join(config.BASE_DIR, "web")

# Blob keys are sha256 hex + optional short extension. Anything else is not a key we
# wrote, and must never reach the filesystem — this is the path-traversal gate.
BLOB_KEY = re.compile(r"^[a-f0-9]{64}(\.[A-Za-z0-9]{1,8})?$")


class HttpError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------- routing --
ROUTES = []


def route(method, pattern):
    """Register a handler. `pattern` is a regex; named groups become handler kwargs."""
    def deco(fn):
        ROUTES.append((method, re.compile("^" + pattern + "$"), fn))
        return fn
    return deco


def needs(level="user"):
    """Auth gate. Handlers declare what they need; nothing else checks roles."""
    def deco(fn):
        @wraps(fn)
        def inner(h, *a, **kw):
            user = h.user
            if not user:
                raise HttpError(401, "not signed in")
            if level == "author" and not auth.can_author(user):
                raise HttpError(403, "forbidden")
            if level == "admin" and not auth.is_admin(user):
                raise HttpError(403, "forbidden")
            return fn(h, *a, **kw)
        return inner
    return deco


# ------------------------------------------------------------------ pages --
def _page(name):
    with open(os.path.join(WEB_DIR, name), "rb") as fh:
        return fh.read()


@route("GET", r"/(chat|studio|admin)?")
def page_app(h, **_):
    if not h.user:
        return h.redirect("/login")
    return h.send(200, _page("index.html"), "text/html; charset=utf-8")


@route("GET", r"/login")
def page_login(h):
    if h.user:
        return h.redirect("/")
    return h.send(200, _page("login.html"), "text/html; charset=utf-8")


@route("GET", r"/logout")
def page_logout(h):
    tok = h.cookie(auth.COOKIE)
    if tok:
        db.delete_session(tok)
    return h.redirect("/login", [("Set-Cookie", f"{auth.COOKIE}=; Path=/; Max-Age=0")])


@route("GET", r"/static/(?P<name>[A-Za-z0-9_.-]+)")
def page_static(h, name):
    path = os.path.join(WEB_DIR, name)
    if not os.path.isfile(path):
        raise HttpError(404, "not found")
    ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
    if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
        ctype += "; charset=utf-8"
    with open(path, "rb") as fh:
        # no-cache (not no-store): the browser revalidates, so editing a file during a
        # demo shows up on reload without disabling caching entirely.
        return h.send(200, fh.read(), ctype, [("Cache-Control", "no-cache")])


@route("GET", r"/blob/(?P<key>[^/]+)")
@needs()
def page_blob(h, key):
    if not BLOB_KEY.match(key):
        raise HttpError(400, "bad blob key")
    try:
        data = mblob.store().get(key)
    except OSError:
        raise HttpError(404, "not found")
    ctype = mimetypes.guess_type(key)[0] or "application/octet-stream"
    return h.send(200, data, ctype, [("Cache-Control", "private, max-age=31536000, immutable")])


# ------------------------------------------------------------------- auth --
@route("POST", r"/api/login")
def api_login(h):
    body = h.json_body()
    u = auth.authenticate(body.get("email", ""), body.get("password", ""))
    if not u:
        return h.json(200, {"ok": False, "error": "Wrong email or password."})
    tok = db.create_session(u["id"])
    return h.json(200, {"ok": True, "role": u["role"]},
                  [("Set-Cookie", f"{auth.COOKIE}={tok}; Path=/; HttpOnly; SameSite=Lax")])


@route("GET", r"/api/me")
@needs()
def api_me(h):
    u = h.user
    return h.json(200, {
        "email": u["email"], "name": u["name"], "role": u["role"],
        "can_author": auth.can_author(u), "is_admin": auth.is_admin(u),
        "provider": settings.effective()["resolved_provider"],
    })


# ---------------------------------------------------------- conversations --
def _title_from(question: str) -> str:
    """Cheap, deterministic titles. A model-generated title would cost a whole extra
    generation per new chat — on a local CPU model that is seconds of dead air."""
    t = " ".join((question or "").split())
    return (t[:47].rstrip() + "…") if len(t) > 48 else (t or "New chat")


@route("GET", r"/api/conversations")
@needs()
def api_convs(h):
    return h.json(200, db.list_conversations(h.user["id"]))


@route("POST", r"/api/conversations")
@needs()
def api_conv_new(h):
    return h.json(200, {"id": db.create_conversation(h.user["id"])})


@route("GET", r"/api/conversations/(?P<cid>[a-f0-9]+)")
@needs()
def api_conv_get(h, cid):
    conv = db.get_conversation(cid, h.user["id"])
    if not conv:
        raise HttpError(404, "no such conversation")
    return h.json(200, {"id": conv["id"], "title": conv["title"],
                        "messages": db.get_messages(cid)})


@route("POST", r"/api/conversations/(?P<cid>[a-f0-9]+)/rename")
@needs()
def api_conv_rename(h, cid):
    db.rename_conversation(cid, h.user["id"], h.json_body().get("title", "").strip() or "New chat")
    return h.json(200, {"ok": True})


@route("POST", r"/api/conversations/(?P<cid>[a-f0-9]+)/delete")
@needs()
def api_conv_delete(h, cid):
    db.delete_conversation(cid, h.user["id"])
    return h.json(200, {"ok": True})


# -------------------------------------------------------------------- ask --
def _profile(user):
    return f"{user['name']} (role: {user['role']})"


@route("POST", r"/api/ask")
@needs()
def api_ask(h):
    body = h.json_body()
    q = (body.get("question") or "").strip()
    if not q:
        raise HttpError(400, "question required")
    return h.json(200, answer(q, profile=_profile(h.user), style=body.get("style", ""),
                              asked_by=h.user["name"]))


@route("POST", r"/api/ask/stream")
@needs()
def api_ask_stream(h):
    body = h.json_body()
    q = (body.get("question") or "").strip()
    if not q:
        raise HttpError(400, "question required")

    user = h.user
    cid = body.get("conversation_id")
    conv = db.get_conversation(cid, user["id"]) if cid else None
    # Whether to name this conversation properly once we've seen the answer. Only the first
    # exchange earns a title: renaming a thread on its fifth message would move it under the
    # reader while they were using it, and a title someone chose by hand must never be
    # overwritten by a model.
    needs_title = False
    if not conv:
        cid = db.create_conversation(user["id"], _title_from(q))
        needs_title = True
    elif not conv["title"] or conv["title"] == "New chat":
        db.rename_conversation(cid, user["id"], _title_from(q))
        needs_title = True

    regenerate = bool(body.get("regenerate"))
    if regenerate:
        db.delete_last_exchange(cid)        # drop the previous answer, keep the question

    # History is everything before this turn — the model must not see the question twice.
    history = [{"role": m["role"], "content": m["content"]} for m in db.get_messages(cid)]
    if regenerate and history and history[-1]["role"] == "user":
        history = history[:-1]
    else:
        db.add_message(cid, "user", q)

    # Past this point the response has begun, so nothing may raise out of here — the
    # generic error handler would try to send a second set of headers into a live stream.
    h.begin_sse()
    h.sse("conversation", {"id": cid, "title": _title_from(q)})

    answer, meta = "", {}
    try:
        for event, payload in answer_stream(q, profile=_profile(user),
                                            style=body.get("style", ""), history=history,
                                            asked_by=user["name"]):
            if event == "delta":
                answer += payload
            elif event == "meta":
                meta = payload
            h.sse(event, payload)
    except (BrokenPipeError, ConnectionResetError):
        pass          # client hit Stop or navigated away — the partial answer is still saved
    except Exception as e:
        print(f"[spacebot] stream failed: {e!r}")
        try:
            h.sse("delta", "\n\nSorry — I hit an error finishing that answer.")
        except OSError:
            pass
    finally:
        if answer.strip():
            db.add_message(cid, "assistant", answer, meta)

    # Name the conversation now the exchange exists, not before it.
    #
    # Titling up front would mean a second model call standing between the user pressing
    # Enter and their first token — seconds of dead air on a local CPU model, to produce a
    # label they aren't looking at yet. Doing it here costs them nothing: the answer is on
    # screen and being read, and the sidebar already shows the question-derived title, so
    # this only ever replaces something serviceable with something better.
    if needs_title and answer.strip() and not regenerate:
        from sb.providers import get_provider
        try:
            better = get_provider().title(q, answer)
        except Exception:
            better = ""
        if better:
            db.rename_conversation(cid, user["id"], better)
            try:
                h.sse("title", {"id": cid, "title": better})
            except OSError:
                pass          # they navigated away; the title is saved either way
    try:
        h.sse("done", {})
    except OSError:
        pass
    return True


# ---------------------------------------------------------- authoring/ops --
@route("GET", r"/api/catalog")
@needs("author")
def api_catalog(h):
    return h.json(200, db.list_workflows())


@route("GET", r"/api/gaps")
@needs("author")
def api_gaps(h):
    return h.json(200, db.list_gaps())


@route("GET", r"/api/jobs/(?P<jid>[a-f0-9]+)")
@needs("author")
def api_job(h, jid):
    return h.json(200, db.get_job(jid) or {})


@route("POST", r"/api/workflows/(?P<key>[^/]+)/publish")
@needs("author")
def api_publish(h, key):
    db.set_workflow_status(key, "published")
    # Publishing is what makes a workflow retrievable, so it is also when it gets indexed.
    # Embedding runs inline: a few hundred milliseconds for a typical document, and it
    # means the first question after Publish is already answerable semantically.
    row = next((c for c in db.get_catalog() if c["wf_key"] == key), None)
    indexed = chunks.reindex_workflow(row["id"]) if row else 0
    embedded = chunks.embed_pending()
    retrieval_invalidate()
    db.log_audit(h.user["name"], h.user["email"], "published", key, f"{indexed} chunks")
    return h.json(200, {"ok": True, "chunks": indexed, "embedded": embedded})


def retrieval_invalidate():
    from sb import retrieval
    retrieval.invalidate()


@route("GET", r"/api/index/status")
@needs("author")
def api_index_status(h):
    from sb import embed
    return h.json(200, {**chunks.stats(), "embedder": embed.get_embedder().health()})


@route("POST", r"/api/index/rebuild")
@needs("admin")
def api_index_rebuild(h):
    n = chunks.reindex_all()
    out = {"chunks": n}
    for _ in range(40):                      # bounded so one call can't run forever
        r = chunks.embed_pending()
        out["embedded"] = out.get("embedded", 0) + r.get("embedded", 0)
        if r.get("error"):
            out["error"] = r["error"]
            break
        if not r.get("pending"):
            break
    retrieval_invalidate()
    return h.json(200, {**out, **chunks.stats()})


@route("POST", r"/api/feed")
@needs("author")
def api_feed(h):
    b = h.json_body()
    try:
        return h.json(200, ingest.structure_from_text(
            b.get("wf_key", "").strip(), b.get("name", ""), b.get("category", ""),
            b.get("owner", "") or h.user["name"], b.get("text", "")))
    except Exception as e:
        raise HttpError(400, str(e))


@route("POST", r"/api/ingest")
@needs("author")
def api_ingest(h):
    parts = h.multipart()
    fields = {p["name"]: p["data"].decode("utf-8", "replace")
              for p in parts if p.get("name") and not p.get("filename")}
    files = [{"filename": p["filename"], "mime": p["content_type"], "bytes": p["data"]}
             for p in parts if p.get("filename")]
    if fields.get("text", "").strip():
        files.append({"filename": "pasted.txt", "mime": "text/plain",
                      "bytes": fields["text"].encode("utf-8")})
    if not fields.get("wf_key", "").strip() or not files:
        raise HttpError(400, "A workflow ID and at least one file (or some text) are required.")
    wf_key = fields["wf_key"].strip()
    job_id = mpipe.start_ingest(wf_key, fields.get("name", ""),
                                fields.get("category", ""),
                                fields.get("owner", "") or h.user["name"], files,
                                actor=h.user["name"])
    db.log_audit(h.user["name"], h.user["email"], "ingested", wf_key,
                 f"{len(files)} file(s)")
    return h.json(200, {"job_id": job_id})


# ------------------------------------------------------------- knowledge --
@route("GET", r"/api/workflows/(?P<key>[^/]+)")
@needs("author")
def api_workflow_get(h, key):
    """The full entry, for a senior reviewing what's actually published."""
    card = next((c for c in db.list_workflows() if c["wf_key"] == key), None)
    if not card:
        raise HttpError(404, "no such workflow")
    pkg = db.get_package(card["id"]) or {}
    return h.json(200, {**pkg, "status": card["status"],
                        "created_by": card.get("created_by", ""),
                        "updated_by": card.get("updated_by", ""),
                        "updated_at": card.get("updated_at")})


@route("POST", r"/api/workflows/(?P<key>[^/]+)/update")
@needs("author")
def api_workflow_update(h, key):
    body = h.json_body()
    fields = {k: body.get(k) for k in ("name", "summary", "category", "owner")
              if body.get(k) is not None}
    if not db.update_workflow_meta(key, fields, actor=h.user["name"]):
        raise HttpError(404, "no such workflow, or nothing to change")
    # The name and summary are both embedded into this workflow's chunks, so an edit that
    # skipped reindexing would leave retrieval matching against the old wording.
    card = next((c for c in db.list_workflows() if c["wf_key"] == key), None)
    if card:
        chunks.reindex_workflow(card["id"])
        chunks.embed_pending()
    retrieval_invalidate()
    db.log_audit(h.user["name"], h.user["email"], "edited", key,
                 ", ".join(sorted(fields)))
    return h.json(200, {"ok": True})


@route("POST", r"/api/workflows/(?P<key>[^/]+)/delete")
@needs("author")
def api_workflow_delete(h, key):
    if not db.delete_workflow(key):
        raise HttpError(404, "no such workflow")
    retrieval_invalidate()
    db.log_audit(h.user["name"], h.user["email"], "deleted", key, "")
    return h.json(200, {"ok": True})


# ------------------------------------------------------------------ audit --
@route("GET", r"/api/admin/overview")
@needs("admin")
def api_admin_overview(h):
    return h.json(200, db.admin_overview())


# --------------------------------------------------------------- settings --
SETTING_KEYS = ("llm_provider", "anthropic_api_key", "openai_api_key", "openai_base_url",
                "ollama_base_url", "route_model", "compose_model", "vision_model")


@route("GET", r"/api/settings")
@needs("admin")
def api_settings_get(h):
    return h.json(200, settings.public_view())


@route("POST", r"/api/settings")
@needs("admin")
def api_settings_set(h):
    body = h.json_body()
    if body.get("ollama_base_url"):
        body["ollama_base_url"] = settings.normalize_ollama_url(body["ollama_base_url"])
    for k in SETTING_KEYS:
        if k in body and body[k] != "":
            db.set_setting(k, body[k])
    # Switching provider must not leave the previous provider's model name behind.
    if body.get("llm_provider") and not body.get("compose_model"):
        for k in ("route_model", "compose_model", "vision_model"):
            db.set_setting(k, "")
    return h.json(200, settings.public_view())


@route("GET", r"/api/model/health")
@needs("admin")
def api_health(h):
    from sb.providers import get_provider
    return h.json(200, get_provider().health())


# ------------------------------------------------------------- multipart ---
def _cd_param(cd, key):
    m = re.search(key + r'="([^"]*)"', cd)
    return m.group(1) if m else None


def parse_multipart(body: bytes, boundary: str) -> list:
    """Minimal multipart/form-data parser (the stdlib lost one when cgi was removed).
    Good enough for the POC; production uploads go presigned direct-to-bucket instead."""
    parts, delim = [], b"--" + boundary.encode()
    for seg in body.split(delim):
        if not seg or seg.startswith(b"--"):
            continue
        if seg[:2] == b"\r\n":
            seg = seg[2:]
        if seg.endswith(b"\r\n"):
            seg = seg[:-2]
        if b"\r\n\r\n" not in seg:
            continue
        head, data = seg.split(b"\r\n\r\n", 1)
        headers = {}
        for line in head.split(b"\r\n"):
            if b":" in line:
                k, v = line.split(b":", 1)
                headers[k.decode("latin1").strip().lower()] = v.decode("latin1").strip()
        cd = headers.get("content-disposition", "")
        parts.append({"name": _cd_param(cd, "name"), "filename": _cd_param(cd, "filename"),
                      "content_type": headers.get("content-type", ""), "data": data})
    return parts


# --------------------------------------------------------------- handler ---
MAX_BODY = 64 * 1024 * 1024      # generous for screenshot batches, bounded so we can't OOM


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Spacebot"
    sys_version = ""

    def log_message(self, *a):
        pass

    # -- request helpers ---------------------------------------------------
    @property
    def user(self):
        if not hasattr(self, "_user"):
            self._user = db.get_session_user(self.cookie(auth.COOKIE))
        return self._user

    def cookie(self, name):
        for part in (self.headers.get("Cookie", "") or "").split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                if k == name:
                    return v
        return None

    def raw_body(self):
        if not hasattr(self, "_raw"):
            n = int(self.headers.get("Content-Length", 0) or 0)
            if n > MAX_BODY:
                raise HttpError(413, "upload too large")
            self._raw = self.rfile.read(n) if n else b""
        return self._raw

    def json_body(self):
        raw = self.raw_body()
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError as e:
            raise HttpError(400, f"bad json: {e}")

    def multipart(self):
        ctype = self.headers.get("Content-Type", "")
        if "boundary=" not in ctype:
            raise HttpError(400, "expected multipart/form-data")
        boundary = ctype.split("boundary=", 1)[-1].strip().strip('"')
        return parse_multipart(self.raw_body(), boundary)

    # -- response helpers --------------------------------------------------
    def send(self, code, body, ctype="application/json", extra=None):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for kv in (extra or []):
            self.send_header(*kv)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)
        return True

    def json(self, code, obj, extra=None):
        return self.send(code, json.dumps(obj), "application/json; charset=utf-8", extra)

    def redirect(self, loc, extra=None):
        self.send_response(302)
        self.send_header("Location", loc)
        self.send_header("Content-Length", "0")
        for kv in (extra or []):
            self.send_header(*kv)
        self.end_headers()
        return True

    def begin_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()

    def sse(self, event, data):
        self.wfile.write(f"event: {event}\ndata: {json.dumps(data)}\n\n".encode("utf-8"))
        self.wfile.flush()

    # -- dispatch ----------------------------------------------------------
    def _dispatch(self, method):
        # HTTP/1.1 keep-alive reuses one handler instance for several requests, so
        # per-request caches MUST be cleared here or request N+1 replays request N's body.
        for attr in ("_user", "_raw"):
            if hasattr(self, attr):
                delattr(self, attr)

        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        allowed = False
        for m, rx, fn in ROUTES:
            match = rx.match(path)
            if not match:
                continue
            if m != method:
                allowed = True
                continue
            try:
                return fn(self, **match.groupdict())
            except HttpError as e:
                return self._fail(path, e.code, e.message)
            except (BrokenPipeError, ConnectionResetError):
                return
            except Exception as e:                       # never leak a traceback to the client
                print(f"[spacebot] {method} {path} failed: {e!r}")
                return self._fail(path, 500, "internal error")
            finally:
                self._drain()
        try:
            return self._fail(path, 405 if allowed else 404,
                              "method not allowed" if allowed else "not found")
        finally:
            self._drain()

    def _drain(self):
        """Consume any request body the handler didn't read.

        On a keep-alive connection the next request is parsed from wherever the last one
        stopped reading. A handler that ignores its body — /delete and /publish take their
        arguments from the URL — leaves those bytes in the socket, and the following
        request line parses as `{}POST /api/... HTTP/1.1`, which the stdlib rejects with
        "Unsupported method ('{}POST')". Every POST after the first on that connection
        fails with a 501.

        Draining unconditionally is the fix, rather than making every handler read a body
        it doesn't want.
        """
        if hasattr(self, "_raw"):
            return                       # handler already consumed it
        try:
            n = int(self.headers.get("Content-Length", 0) or 0)
        except (TypeError, ValueError):
            n = 0
        if n > 0:
            try:
                self.rfile.read(n)
            except OSError:
                pass
        self._raw = b""

    def _fail(self, path, code, message):
        if path.startswith("/api/"):
            return self.json(code, {"error": message})
        if code in (401, 403):
            return self.redirect("/login")
        return self.send(code, message, "text/plain; charset=utf-8")

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")


def main():
    db.init_db()
    n = len(db.get_catalog())
    s = settings.effective()
    prov = s["resolved_provider"]

    print(f"\n  🛰  Spacebot  →  http://localhost:{PORT}")
    print(f"      {n} published workflow{'' if n == 1 else 's'}  ·  model: {prov}", end="")
    if prov == "ollama":
        print(f" ({s['compose_model']}) at {s['ollama_base_url']}")
    else:
        print()
    if prov == "mock":
        print("      No model reachable — running the offline heuristic.")
        print("      Start Ollama and pull a model for real answers:")
        print("        ollama serve  &&  ollama pull llama3.2:3b")
    # Checked by count, not by a named account: hardcoding one demo login here meant that
    # renaming the demo users left the server permanently advising a reseed it did not need.
    if n == 0 or db.count_users() == 0:
        print("      Nobody can sign in yet. Either:")
        print("        python3 setup.py --org 'Your Company' --admin you@example.com")
        print("        python3 seed.py            (demo data + demo logins)")
    print()

    try:
        ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
    except OSError as e:
        # "Address already in use" is the most common way this program fails, and a raw
        # traceback makes it look like the app is broken when in fact it is already
        # running. Say which process, and give the two ways out.
        if e.errno != errno.EADDRINUSE:
            raise
        print(f"\n  Port {PORT} is already in use — Spacebot may already be running.")
        print(f"      Open http://localhost:{PORT} to check.\n")
        holder = ""
        try:
            out = subprocess.run(["ss", "-lptnH", f"sport = :{PORT}"],
                                 capture_output=True, text=True, timeout=5).stdout
            m = re.search(r"pid=(\d+)", out)
            holder = m.group(1) if m else ""
        except (OSError, subprocess.SubprocessError):
            pass
        if holder:
            print(f"      Held by process {holder}. To take the port over:")
            print(f"        kill {holder} && python3 server.py")
        else:
            print("      To find and stop whatever holds it:")
            print(f"        ss -lptn 'sport = :{PORT}'")
        print(f"\n      Or run this copy somewhere else:")
        print(f"        SPACEBOT_PORT=8081 python3 server.py\n")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
