"""SQLite storage layer.

Everything is workflow-scoped: every asset, step, known-error and FAQ carries a
workflow_id, and retrieval always starts from a workflow. That containment is the
main reason a rollback question can never pull in a procurement document.

SQLite is the POC store. The read/write helpers below are the only surface the rest
of the app uses, so swapping to Postgres+pgvector later is a matter of reimplementing
this file, not the pipeline.
"""
import json
import os
import sqlite3
import time
import uuid

from . import config


def _id() -> str:
    return uuid.uuid4().hex[:12]


_migrated = False


def connect() -> sqlite3.Connection:
    """Open the database, bringing its schema up to date the first time in this process.

    Migrations run here rather than only in `init_db` because queries do not wait for
    anyone to remember. `list_workflows` selects `created_by`, a column added after the
    first release; a script that imported this module and queried without calling `init_db`
    got `sqlite3.OperationalError: no such column`. That is not a test-only problem — it is
    exactly what an existing deployment does when it pulls new code and runs a tool.

    The flag is set before the attempt, so a migration that cannot apply fails once rather
    than on every connection for the life of the process.
    """
    global _migrated
    os.makedirs(os.path.dirname(config.DB_PATH), exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if not _migrated:
        _migrated = True
        try:
            _migrate(conn)
            conn.commit()
        except sqlite3.Error:
            pass          # brand-new file with no tables yet; init_db creates them
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    email TEXT UNIQUE,
    name TEXT,
    role TEXT,                 -- user | author | admin
    pw TEXT,                   -- salt$pbkdf2 hash
    created_at REAL
);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id TEXT,
    created_at REAL
);
CREATE TABLE IF NOT EXISTS workflows (
    id TEXT PRIMARY KEY,
    wf_key TEXT UNIQUE,
    name TEXT,
    summary TEXT,
    category TEXT,
    owner TEXT,
    status TEXT DEFAULT 'published',
    version INTEGER DEFAULT 1,
    trigger_phrases TEXT DEFAULT '[]',
    subjects TEXT DEFAULT '[]',        -- who/what this entry is about, named at ingest
    verified_by TEXT,
    verified_at TEXT,
    created_at REAL
);
CREATE TABLE IF NOT EXISTS steps (
    id TEXT PRIMARY KEY,
    workflow_id TEXT REFERENCES workflows(id) ON DELETE CASCADE,
    order_index INTEGER,
    title TEXT,
    body TEXT,
    verification TEXT,
    tips TEXT DEFAULT '[]',
    mistakes TEXT DEFAULT '[]',
    asset_id TEXT,
    clip_start INTEGER
);
CREATE TABLE IF NOT EXISTS assets (
    id TEXT PRIMARY KEY,
    workflow_id TEXT REFERENCES workflows(id) ON DELETE CASCADE,
    kind TEXT,                 -- pdf | image | video | transcript | text
    filename TEXT,
    text TEXT,                 -- extracted / OCR / transcript / vision description
    source_ref TEXT DEFAULT '{}',
    created_at REAL
);
CREATE TABLE IF NOT EXISTS known_errors (
    id TEXT PRIMARY KEY,
    workflow_id TEXT REFERENCES workflows(id) ON DELETE CASCADE,
    code TEXT,
    cause TEXT,
    resolution TEXT
);
CREATE TABLE IF NOT EXISTS faqs (
    id TEXT PRIMARY KEY,
    workflow_id TEXT REFERENCES workflows(id) ON DELETE CASCADE,
    question TEXT,
    answer TEXT
);
CREATE TABLE IF NOT EXISTS relations (
    from_id TEXT,
    to_id TEXT,
    kind TEXT                  -- prerequisite | next | related | alternative
);
CREATE TABLE IF NOT EXISTS ask_log (
    id TEXT PRIMARY KEY,
    question TEXT,
    route TEXT,
    workflow_ids TEXT,
    answer TEXT,
    confidence REAL,
    abstained INTEGER,
    provider TEXT,
    created_at REAL
);
CREATE TABLE IF NOT EXISTS gaps (
    id TEXT PRIMARY KEY,
    question TEXT,
    count INTEGER DEFAULT 1,
    status TEXT DEFAULT 'open',
    created_at REAL
);
-- Who changed what, and when. One row per change to the knowledge base — the whole audit
-- trail, deliberately.
--
-- Knowledge is the thing people act on here, so "who last touched this and when" is the
-- question an admin actually has when an answer turns out to be wrong. Logging reads as
-- well would bury that in noise; retrieval usage is already recoverable from ask_log,
-- which records the workflows behind every answer.
CREATE TABLE IF NOT EXISTS audit_log (
    id TEXT PRIMARY KEY,
    at REAL,
    actor TEXT,            -- display name of whoever did it
    actor_email TEXT,
    action TEXT,           -- ingested | published | edited | deleted | reindexed
    wf_key TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_at ON audit_log(at DESC);
-- ---- chat history ----------------------------------------------------
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    user_id TEXT,
    title TEXT,
    created_at REAL,
    updated_at REAL
);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT,                 -- user | assistant
    content TEXT,
    meta TEXT DEFAULT '{}',    -- the answer's workflow/band/confidence, for re-rendering
    created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_msg_conv ON messages(conversation_id, created_at);
CREATE INDEX IF NOT EXISTS idx_conv_user ON conversations(user_id, updated_at DESC);
-- ---- multimodal ingestion --------------------------------------------
CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY,
    job_id TEXT,
    workflow_id TEXT,
    kind TEXT,                 -- pdf | image | video | audio | transcript
    filename TEXT,
    mime TEXT,
    blob_key TEXT,             -- content-addressed key in the blob store
    checksum TEXT,
    bytes INTEGER,
    status TEXT,               -- stored | decomposed | failed | awaiting_capability
    meta TEXT DEFAULT '{}',
    order_index INTEGER,       -- order this source was uploaded in the batch
    created_at REAL
);
CREATE TABLE IF NOT EXISTS segments (
    id TEXT PRIMARY KEY,
    source_id TEXT,
    job_id TEXT,
    workflow_id TEXT,
    modality TEXT,             -- text | image | pdf_page | video_scene | audio
    order_index INTEGER,       -- global order across the whole batch
    text TEXT,                 -- extracted / OCR / ASR / vision description
    image_blob_key TEXT,       -- the representative visual, if any
    anchor TEXT DEFAULT '{}',  -- {page:N} | {image_index:N} | {t_start,t_end}
    meta TEXT DEFAULT '{}',    -- {contains_secret, speaker, ui_elements...}
    created_at REAL
);
CREATE TABLE IF NOT EXISTS ingestion_jobs (
    id TEXT PRIMARY KEY,
    workflow_id TEXT,
    wf_key TEXT,
    status TEXT,               -- queued | running | structuring | drafted | failed
    stage TEXT,                -- human-readable progress line
    source_count INTEGER DEFAULT 0,
    result TEXT DEFAULT '{}',
    error TEXT,
    created_at REAL,
    updated_at REAL
);
"""


def init_db():
    conn = connect()
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    conn.close()


# Columns added after the first release. `CREATE TABLE IF NOT EXISTS` above only builds
# fresh databases, so an existing deployment upgrading the code would otherwise keep a
# table without them and fail on the first read. Each entry is idempotent: SQLite raises
# if the column is already there, which is the signal to move on.
_ADDED_COLUMNS = [
    ("workflows", "subjects", "TEXT DEFAULT '[]'"),
    ("workflows", "created_by", "TEXT DEFAULT ''"),
    ("workflows", "updated_by", "TEXT DEFAULT ''"),
    ("workflows", "updated_at", "REAL"),
    ("ask_log", "asked_by", "TEXT DEFAULT ''"),
    ("chunks", "source_id", "TEXT"),
    ("chunks", "segment_id", "TEXT"),
    ("chunks", "anchor", "TEXT DEFAULT '{}'"),
]


def _migrate(conn):
    for table, column, decl in _ADDED_COLUMNS:
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        except sqlite3.OperationalError:
            pass          # already present


# ---- settings -------------------------------------------------------------
def get_settings_dict() -> dict:
    conn = connect()
    try:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {r["key"]: r["value"] for r in rows}
    finally:
        conn.close()


def set_setting(key: str, value: str):
    conn = connect()
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()
    conn.close()


# ---- workflow authoring ---------------------------------------------------
def upsert_workflow(wf: dict) -> str:
    """wf: {wf_key,name,summary,category,owner,trigger_phrases,steps,known_errors,faqs}"""
    conn = connect()
    cur = conn.cursor()
    row = cur.execute("SELECT id, subjects FROM workflows WHERE wf_key=?",
                      (wf["wf_key"],)).fetchone()
    wid = row["id"] if row else _id()

    # Subjects ACCUMULATE across writes rather than replacing.
    #
    # They are named by a model reading the document, and a model asked the same question
    # twice does not answer the same way twice. Re-ingesting the on-call rota turned
    # ["Oncall Schedule", "Sarah", "Arjun", "Meena", "Priya"] into ["Oncall", "Schedule"] —
    # it named the document instead of the people in it, and every person the rota is
    # about stopped being attributable. The vendor list lost Meena the same way.
    #
    # Merging trades a bounded cost for an unbounded one. An extra subject only widens what
    # a question can match, and matching is a boost rather than a filter, so the damage is
    # a slightly less focused ranking. A subject silently dropped removes the attribution
    # that stops one person's question being answered out of another person's file, and
    # nothing anywhere reports it.
    incoming = wf.get("subjects") or []
    if row:
        merged = list(json.loads(row["subjects"] or "[]"))
        seen = {s.casefold() for s in merged}
        for s in incoming:
            if s.casefold() not in seen:
                seen.add(s.casefold())
                merged.append(s)
        subjects_json = json.dumps(merged[:12])
    else:
        subjects_json = json.dumps(incoming[:12])
    if row:
        cur.execute("DELETE FROM steps WHERE workflow_id=?", (wid,))
        cur.execute("DELETE FROM known_errors WHERE workflow_id=?", (wid,))
        cur.execute("DELETE FROM faqs WHERE workflow_id=?", (wid,))
    cur.execute(
        """INSERT INTO workflows(id,wf_key,name,summary,category,owner,status,version,
               trigger_phrases,subjects,verified_by,verified_at,created_at,
               created_by,updated_by,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
               name=excluded.name, summary=excluded.summary, category=excluded.category,
               owner=excluded.owner, status=excluded.status, version=version+1,
               trigger_phrases=excluded.trigger_phrases, subjects=excluded.subjects,
               verified_by=excluded.verified_by, verified_at=excluded.verified_at,
               -- created_by is written once and never overwritten. It answers "who brought
               -- this into the knowledge base", which a later edit does not change.
               updated_by=excluded.updated_by, updated_at=excluded.updated_at""",
        (wid, wf["wf_key"], wf["name"], wf.get("summary", ""), wf.get("category", ""),
         wf.get("owner", ""), wf.get("status", "published"), 1,
         json.dumps(wf.get("trigger_phrases", [])),
         subjects_json,
         wf.get("verified_by", ""), wf.get("verified_at", ""), time.time(),
         wf.get("actor", ""), wf.get("actor", ""), time.time()),
    )
    for i, s in enumerate(wf.get("steps", [])):
        cur.execute(
            """INSERT INTO steps(id,workflow_id,order_index,title,body,verification,tips,mistakes,asset_id,clip_start)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (_id(), wid, i + 1, s.get("title", ""), s.get("body", ""), s.get("verification", ""),
             json.dumps(s.get("tips", [])), json.dumps(s.get("mistakes", [])),
             s.get("asset_id"), s.get("clip_start")),
        )
    for e in wf.get("known_errors", []):
        cur.execute("INSERT INTO known_errors(id,workflow_id,code,cause,resolution) VALUES(?,?,?,?,?)",
                    (_id(), wid, e.get("code", ""), e.get("cause", ""), e.get("resolution", "")))
    for f in wf.get("faqs", []):
        cur.execute("INSERT INTO faqs(id,workflow_id,question,answer) VALUES(?,?,?,?)",
                    (_id(), wid, f.get("question", ""), f.get("answer", "")))
    conn.commit()
    conn.close()
    return wid


def add_asset(workflow_id: str, kind: str, filename: str, text: str, source_ref: dict = None) -> str:
    conn = connect()
    aid = _id()
    conn.execute("INSERT INTO assets(id,workflow_id,kind,filename,text,source_ref,created_at) VALUES(?,?,?,?,?,?,?)",
                 (aid, workflow_id, kind, filename, text, json.dumps(source_ref or {}), time.time()))
    conn.commit()
    conn.close()
    return aid


def add_relation(from_key: str, to_key: str, kind: str):
    conn = connect()
    a = conn.execute("SELECT id FROM workflows WHERE wf_key=?", (from_key,)).fetchone()
    b = conn.execute("SELECT id FROM workflows WHERE wf_key=?", (to_key,)).fetchone()
    if a and b:
        conn.execute("INSERT INTO relations(from_id,to_id,kind) VALUES(?,?,?)", (a["id"], b["id"], kind))
        conn.commit()
    conn.close()


# ---- retrieval reads ------------------------------------------------------
CARD_TRIGGER_LIMIT = 14


def get_catalog() -> list:
    """Compact cards for the router — one row per workflow, cheap to put in a prompt.

    FAQ questions are folded into the trigger phrases because they are literally "questions
    this entry can answer" — the best routing signal we have. It matters most for reference
    entries (a CV, a policy) whose summary can't possibly mention everything they cover:
    without this, "where did yaswanth study?" routes nowhere even though the answer is
    sitting in an FAQ.
    """
    conn = connect()
    rows = conn.execute(
        "SELECT id,wf_key,name,summary,category,owner,trigger_phrases "
        "FROM workflows WHERE status='published'").fetchall()
    faqs = {}
    for f in conn.execute("SELECT workflow_id, question FROM faqs").fetchall():
        faqs.setdefault(f["workflow_id"], []).append(f["question"])
    conn.close()

    cards = []
    for r in rows:
        triggers = json.loads(r["trigger_phrases"] or "[]")
        seen = {t.lower() for t in triggers}
        for q in faqs.get(r["id"], []):
            if q and q.lower() not in seen:
                seen.add(q.lower())
                triggers.append(q)
        cards.append({
            "id": r["id"], "wf_key": r["wf_key"], "name": r["name"],
            "summary": r["summary"], "category": r["category"], "owner": r["owner"],
            "trigger_phrases": triggers[:CARD_TRIGGER_LIMIT],
        })
    return cards


def all_known_error_codes() -> list:
    conn = connect()
    rows = conn.execute("SELECT ke.code, w.id AS wid, w.wf_key FROM known_errors ke "
                        "JOIN workflows w ON w.id=ke.workflow_id").fetchall()
    conn.close()
    return [{"code": r["code"], "workflow_id": r["wid"], "wf_key": r["wf_key"]} for r in rows]


def get_package(workflow_id: str) -> dict:
    """The full workflow package — the ONLY context the composer sees for this workflow."""
    conn = connect()
    w = conn.execute("SELECT * FROM workflows WHERE id=?", (workflow_id,)).fetchone()
    if not w:
        conn.close()
        return None
    steps = conn.execute("SELECT * FROM steps WHERE workflow_id=? ORDER BY order_index", (workflow_id,)).fetchall()
    assets = {a["id"]: a for a in conn.execute("SELECT * FROM assets WHERE workflow_id=?", (workflow_id,)).fetchall()}
    kes = conn.execute("SELECT * FROM known_errors WHERE workflow_id=?", (workflow_id,)).fetchall()
    faqs = conn.execute("SELECT * FROM faqs WHERE workflow_id=?", (workflow_id,)).fetchall()
    rels = conn.execute(
        "SELECT r.kind, w2.wf_key, w2.name FROM relations r "
        "JOIN workflows w2 ON w2.id=r.to_id WHERE r.from_id=?", (workflow_id,)).fetchall()
    conn.close()

    def step_dict(s):
        a = assets.get(s["asset_id"])
        return {
            "key": f"step-{s['order_index']}",
            "order": s["order_index"], "title": s["title"], "body": s["body"],
            "verification": s["verification"],
            "tips": json.loads(s["tips"] or "[]"),
            "mistakes": json.loads(s["mistakes"] or "[]"),
            "asset": ({"kind": a["kind"], "filename": a["filename"], "text": a["text"]} if a else None),
            "clip_start": s["clip_start"],
        }

    return {
        "id": w["id"], "wf_key": w["wf_key"], "name": w["name"], "summary": w["summary"],
        "category": w["category"], "owner": w["owner"], "version": w["version"],
        "verified_by": w["verified_by"], "verified_at": w["verified_at"],
        "trigger_phrases": json.loads(w["trigger_phrases"] or "[]"),
        "subjects": json.loads((w["subjects"] if "subjects" in w.keys() else "") or "[]"),
        "steps": [step_dict(s) for s in steps],
        "extra_assets": [{"kind": a["kind"], "filename": a["filename"], "text": a["text"],
                          "structure": json.loads(a["source_ref"] or "{}").get("structure"),
                          "source_id": json.loads(a["source_ref"] or "{}").get("source_id")}
                         for a in assets.values() if a["id"] not in {s["asset_id"] for s in steps}],
        "known_errors": [{"code": e["code"], "cause": e["cause"], "resolution": e["resolution"]} for e in kes],
        "faqs": [{"question": f["question"], "answer": f["answer"]} for f in faqs],
        "related": [{"kind": r["kind"], "wf_key": r["wf_key"], "name": r["name"]} for r in rels],
    }


def list_workflows() -> list:
    conn = connect()
    rows = conn.execute(
        "SELECT w.id,w.wf_key,w.name,w.summary,w.category,w.owner,w.verified_by,w.status, "
        "w.created_by,w.updated_by,w.updated_at,w.created_at, "
        "(SELECT COUNT(*) FROM steps s WHERE s.workflow_id=w.id) AS step_count, "
        "(SELECT COUNT(*) FROM assets a WHERE a.workflow_id=w.id) AS asset_count "
        "FROM workflows w ORDER BY w.category, w.name").fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---- learning loop --------------------------------------------------------
def log_ask(question, route, workflow_ids, answer, confidence, abstained, provider,
            asked_by=""):
    conn = connect()
    conn.execute(
        "INSERT INTO ask_log(id,question,route,workflow_ids,answer,confidence,abstained,"
        "provider,created_at,asked_by) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (_id(), question, json.dumps(route), json.dumps(workflow_ids), json.dumps(answer),
         confidence, 1 if abstained else 0, provider, time.time(), asked_by or ""))
    conn.commit()
    conn.close()


_eta_cache = {"at": 0.0, "value": None}


def typical_answer_seconds(default: float = 45.0) -> float:
    """How long an answer usually takes on THIS machine, from what actually happened.

    Used to drive the progress bar. A hardcoded guess would be wrong on every machine but
    the one it was written on — a laptop on a 3B and a server on a 7B differ by minutes —
    whereas the median of recent answers is right by construction and adapts as the corpus
    or the model changes.

    Median rather than mean: one cold start where the model had to load from disk should not
    stretch every subsequent estimate.

    Cached for a minute. This is read on every question and its answer moves slowly.
    """
    now = time.time()
    if _eta_cache["value"] is not None and now - _eta_cache["at"] < 60:
        return _eta_cache["value"]

    times = []
    try:
        conn = connect()
        rows = conn.execute(
            "SELECT answer FROM ask_log ORDER BY created_at DESC LIMIT 40").fetchall()
        conn.close()
        for r in rows:
            v = (json.loads(r["answer"] or "{}") or {}).get("elapsed")
            if isinstance(v, (int, float)) and 0 < v < 900:
                times.append(v)
    except (sqlite3.Error, ValueError, TypeError):
        pass

    times.sort()
    value = times[len(times) // 2] if times else default
    _eta_cache.update({"at": now, "value": value})
    return value


def update_workflow_meta(wf_key: str, fields: dict, actor: str = "") -> bool:
    """Edit the parts of a workflow a senior owns: its name, summary, category and owner.

    Deliberately not the steps. Those came out of the source document, and letting them be
    edited here would create knowledge with no provenance — the audit trail could say who
    changed it but not what it was grounded in. Re-ingesting the corrected document is the
    honest path for a content change.
    """
    allowed = {k: v for k, v in fields.items()
               if k in ("name", "summary", "category", "owner") and v is not None}
    if not allowed:
        return False
    sets = ", ".join(f"{k}=?" for k in allowed)
    conn = connect()
    cur = conn.execute(
        f"UPDATE workflows SET {sets}, updated_by=?, updated_at=? WHERE wf_key=?",
        (*allowed.values(), actor or "", time.time(), wf_key))
    changed = cur.rowcount > 0
    conn.commit()
    conn.close()
    return changed


def delete_workflow(wf_key: str) -> bool:
    """Remove a workflow and everything hanging off it, chunks included.

    The chunk delete is the part that matters: leave those behind and the document keeps
    answering questions after it has been deleted, which is the worst possible outcome for
    a knowledge base someone has just decided is wrong.
    """
    conn = connect()
    row = conn.execute("SELECT id FROM workflows WHERE wf_key=?", (wf_key,)).fetchone()
    if not row:
        conn.close()
        return False
    wid = row["id"]
    for table in ("steps", "known_errors", "faqs", "assets", "chunks"):
        try:
            conn.execute(f"DELETE FROM {table} WHERE workflow_id=?", (wid,))
        except sqlite3.OperationalError:
            pass                     # chunks table may not exist yet on a fresh install
    conn.execute("DELETE FROM relations WHERE from_id=? OR to_id=?", (wid, wid))
    conn.execute("DELETE FROM workflows WHERE id=?", (wid,))
    conn.commit()
    conn.close()
    return True


# ---- audit ----------------------------------------------------------------
def log_audit(actor: str, actor_email: str, action: str, wf_key: str, detail: str = ""):
    """Record a change to the knowledge base. Never raises.

    Auditing must not be able to break the thing it observes: a failure to write history
    should not fail a publish. So this swallows its own errors — a missing audit row is a
    gap in a report, a failed publish is a person unable to do their job.
    """
    try:
        conn = connect()
        conn.execute(
            "INSERT INTO audit_log(id,at,actor,actor_email,action,wf_key,detail) "
            "VALUES(?,?,?,?,?,?,?)",
            (_id(), time.time(), actor or "", actor_email or "", action, wf_key or "",
             detail or ""))
        conn.commit()
        conn.close()
    except sqlite3.Error:
        pass


def recent_audit(limit: int = 25) -> list:
    conn = connect()
    rows = conn.execute(
        "SELECT at,actor,actor_email,action,wf_key,detail FROM audit_log "
        "ORDER BY at DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def top_artifacts(limit: int = 8, days: int = 30) -> list:
    """Which knowledge actually gets used to answer people.

    Counted from ask_log, which already records the workflows behind every answer, so this
    needs no new instrumentation. It is the one usage number worth showing: it tells a
    senior which documents are load-bearing (and so worth keeping accurate) and which have
    never answered anything since the day they were added.

    Abstained answers are excluded — those cite nothing and would otherwise inflate whatever
    happened to rank during a question we could not answer.
    """
    since = time.time() - days * 86400
    conn = connect()
    rows = conn.execute(
        "SELECT workflow_ids FROM ask_log WHERE created_at >= ? AND abstained = 0",
        (since,)).fetchall()
    names = {w["wf_key"]: w["name"] for w in conn.execute(
        "SELECT wf_key,name FROM workflows").fetchall()}
    conn.close()

    counts = {}
    for r in rows:
        try:
            keys = json.loads(r["workflow_ids"] or "[]")
        except (ValueError, TypeError):
            continue
        for k in dict.fromkeys(keys):        # one document, one count, per question
            counts[k] = counts.get(k, 0) + 1

    out = [{"wf_key": k, "name": names.get(k, k), "uses": n} for k, n in counts.items()]
    out.sort(key=lambda x: (-x["uses"], x["name"]))
    return out[:limit]


def admin_overview(days: int = 30) -> dict:
    """Everything the admin screen shows, in one query pass."""
    since = time.time() - days * 86400
    conn = connect()

    wf = conn.execute(
        "SELECT COUNT(*) total, SUM(status='published') published FROM workflows").fetchone()
    asks = conn.execute(
        "SELECT COUNT(*) total, SUM(abstained) abstained FROM ask_log "
        "WHERE created_at >= ?", (since,)).fetchone()
    gaps_open = conn.execute(
        "SELECT COUNT(*) c FROM gaps WHERE status='open'").fetchone()["c"]
    people = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]

    # Documents nobody has ever been answered from. The useful inverse of top_artifacts:
    # knowledge that exists, is published, and has never once helped anyone.
    used = set()
    for r in conn.execute("SELECT workflow_ids FROM ask_log WHERE abstained = 0").fetchall():
        try:
            used.update(json.loads(r["workflow_ids"] or "[]"))
        except (ValueError, TypeError):
            pass
    unused = [dict(r) for r in conn.execute(
        "SELECT wf_key,name,owner FROM workflows WHERE status='published'").fetchall()
        if r["wf_key"] not in used]

    recent_q = [dict(r) for r in conn.execute(
        "SELECT question,asked_by,abstained,confidence,created_at FROM ask_log "
        "ORDER BY created_at DESC LIMIT 12").fetchall()]

    # Median, not mean: one cold-start answer that took four minutes should not become the
    # number an operator reads as "typical".
    times = []
    for r in conn.execute("SELECT answer FROM ask_log WHERE created_at >= ?",
                          (since,)).fetchall():
        try:
            v = (json.loads(r["answer"] or "{}") or {}).get("elapsed")
        except (ValueError, TypeError):
            v = None
        if isinstance(v, (int, float)):
            times.append(v)
    times.sort()
    median_s = times[len(times) // 2] if times else None
    conn.close()

    total_asks = asks["total"] or 0
    abstained = asks["abstained"] or 0
    return {
        "days": days,
        "median_seconds": median_s,
        "workflows": wf["total"] or 0,
        "published": wf["published"] or 0,
        "people": people,
        "asks": total_asks,
        "abstained": abstained,
        "answer_rate": round(100 * (total_asks - abstained) / total_asks) if total_asks else None,
        "gaps_open": gaps_open,
        "top_artifacts": top_artifacts(days=days),
        "unused": unused[:8],
        "recent_activity": recent_audit(),
        "recent_questions": recent_q,
    }


def add_gap(question: str):
    conn = connect()
    conn.execute("INSERT INTO gaps(id,question,count,status,created_at) VALUES(?,?,?,?,?)",
                 (_id(), question, 1, "open", time.time()))
    conn.commit()
    conn.close()


def list_gaps() -> list:
    conn = connect()
    rows = conn.execute("SELECT question, count, status, created_at FROM gaps "
                        "ORDER BY created_at DESC LIMIT 50").fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---- conversations & messages ---------------------------------------------
def create_conversation(user_id: str, title: str = "New chat") -> str:
    conn = connect()
    cid = _id()
    now = time.time()
    conn.execute("INSERT INTO conversations(id,user_id,title,created_at,updated_at) VALUES(?,?,?,?,?)",
                 (cid, user_id, title, now, now))
    conn.commit()
    conn.close()
    return cid


def list_conversations(user_id: str, limit: int = 100) -> list:
    conn = connect()
    rows = conn.execute(
        "SELECT c.id, c.title, c.updated_at, "
        "(SELECT COUNT(*) FROM messages m WHERE m.conversation_id=c.id) AS message_count "
        "FROM conversations c WHERE c.user_id=? "
        "AND EXISTS(SELECT 1 FROM messages m WHERE m.conversation_id=c.id) "
        "ORDER BY c.updated_at DESC LIMIT ?", (user_id, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_conversation(conv_id: str, user_id: str):
    """Always scoped by user_id — a conversation id is not an authorisation token."""
    conn = connect()
    r = conn.execute("SELECT * FROM conversations WHERE id=? AND user_id=?",
                     (conv_id, user_id)).fetchone()
    conn.close()
    return dict(r) if r else None


def get_messages(conv_id: str, limit: int = 200) -> list:
    conn = connect()
    rows = conn.execute("SELECT id,role,content,meta,created_at FROM messages "
                        "WHERE conversation_id=? ORDER BY created_at LIMIT ?",
                        (conv_id, limit)).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["meta"] = json.loads(d.get("meta") or "{}")
        except ValueError:
            d["meta"] = {}
        out.append(d)
    return out


def add_message(conv_id: str, role: str, content: str, meta: dict = None) -> str:
    conn = connect()
    mid = _id()
    now = time.time()
    conn.execute("INSERT INTO messages(id,conversation_id,role,content,meta,created_at) VALUES(?,?,?,?,?,?)",
                 (mid, conv_id, role, content, json.dumps(meta or {}), now))
    conn.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, conv_id))
    conn.commit()
    conn.close()
    return mid


def rename_conversation(conv_id: str, user_id: str, title: str):
    conn = connect()
    conn.execute("UPDATE conversations SET title=? WHERE id=? AND user_id=?",
                 (title[:120], conv_id, user_id))
    conn.commit()
    conn.close()


def delete_conversation(conv_id: str, user_id: str):
    conn = connect()
    row = conn.execute("SELECT id FROM conversations WHERE id=? AND user_id=?",
                       (conv_id, user_id)).fetchone()
    if row:
        conn.execute("DELETE FROM messages WHERE conversation_id=?", (conv_id,))
        conn.execute("DELETE FROM conversations WHERE id=?", (conv_id,))
        conn.commit()
    conn.close()


def delete_last_exchange(conv_id: str):
    """Drop the trailing assistant reply (and nothing else) so a regenerate doesn't
    stack two answers to the same question in the history."""
    conn = connect()
    row = conn.execute("SELECT id, role FROM messages WHERE conversation_id=? "
                       "ORDER BY created_at DESC LIMIT 1", (conv_id,)).fetchone()
    if row and row["role"] == "assistant":
        conn.execute("DELETE FROM messages WHERE id=?", (row["id"],))
        conn.commit()
    conn.close()


# ---- users & sessions -----------------------------------------------------
def create_user(email: str, name: str, role: str, pw_hash: str) -> str:
    conn = connect()
    uid = _id()
    try:
        conn.execute("INSERT INTO users(id,email,name,role,pw,created_at) VALUES(?,?,?,?,?,?)",
                     (uid, email.lower(), name, role, pw_hash, time.time()))
        conn.commit()
    except sqlite3.IntegrityError:
        row = conn.execute("SELECT id FROM users WHERE email=?", (email.lower(),)).fetchone()
        uid = row["id"] if row else uid
    finally:
        conn.close()
    return uid


def count_users() -> int:
    conn = connect()
    n = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    conn.close()
    return n


def get_user_by_email(email: str):
    conn = connect()
    r = conn.execute("SELECT * FROM users WHERE email=?", (email.lower(),)).fetchone()
    conn.close()
    return dict(r) if r else None


def create_session(user_id: str) -> str:
    conn = connect()
    token = uuid.uuid4().hex + uuid.uuid4().hex
    conn.execute("INSERT INTO sessions(token,user_id,created_at) VALUES(?,?,?)",
                 (token, user_id, time.time()))
    conn.commit()
    conn.close()
    return token


def get_session_user(token: str):
    if not token:
        return None
    conn = connect()
    r = conn.execute(
        "SELECT u.id,u.email,u.name,u.role FROM sessions s JOIN users u ON u.id=s.user_id "
        "WHERE s.token=?", (token,)).fetchone()
    conn.close()
    return dict(r) if r else None


def delete_session(token: str):
    conn = connect()
    conn.execute("DELETE FROM sessions WHERE token=?", (token,))
    conn.commit()
    conn.close()


# ---- ingestion: sources, segments, jobs -----------------------------------
def create_job(workflow_id, wf_key, source_count) -> str:
    conn = connect()
    jid = _id()
    now = time.time()
    conn.execute("INSERT INTO ingestion_jobs(id,workflow_id,wf_key,status,stage,source_count,created_at,updated_at) "
                 "VALUES(?,?,?,?,?,?,?,?)",
                 (jid, workflow_id, wf_key, "queued", "queued", source_count, now, now))
    conn.commit()
    conn.close()
    return jid


def update_job(job_id, **fields):
    if not fields:
        return
    fields["updated_at"] = time.time()
    if "result" in fields and not isinstance(fields["result"], str):
        fields["result"] = json.dumps(fields["result"])
    cols = ", ".join(f"{k}=?" for k in fields)
    conn = connect()
    conn.execute(f"UPDATE ingestion_jobs SET {cols} WHERE id=?", (*fields.values(), job_id))
    conn.commit()
    conn.close()


def get_job(job_id):
    conn = connect()
    r = conn.execute("SELECT * FROM ingestion_jobs WHERE id=?", (job_id,)).fetchone()
    conn.close()
    if not r:
        return None
    d = dict(r)
    d["result"] = json.loads(d.get("result") or "{}")
    return d


def create_source(job_id, workflow_id, kind, filename, mime, blob_key, checksum, nbytes, order_index, meta=None) -> str:
    conn = connect()
    sid = _id()
    conn.execute("INSERT INTO sources(id,job_id,workflow_id,kind,filename,mime,blob_key,checksum,bytes,status,meta,order_index,created_at) "
                 "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (sid, job_id, workflow_id, kind, filename, mime, blob_key, checksum, nbytes,
                  "stored", json.dumps(meta or {}), order_index, time.time()))
    conn.commit()
    conn.close()
    return sid


def set_source_status(source_id, status, meta=None):
    conn = connect()
    if meta is not None:
        conn.execute("UPDATE sources SET status=?, meta=? WHERE id=?", (status, json.dumps(meta), source_id))
    else:
        conn.execute("UPDATE sources SET status=? WHERE id=?", (status, source_id))
    conn.commit()
    conn.close()


def add_segment(source_id, job_id, workflow_id, modality, order_index, text,
                image_blob_key=None, anchor=None, meta=None) -> str:
    conn = connect()
    sid = _id()
    conn.execute("INSERT INTO segments(id,source_id,job_id,workflow_id,modality,order_index,text,image_blob_key,anchor,meta,created_at) "
                 "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                 (sid, source_id, job_id, workflow_id, modality, order_index, text or "",
                  image_blob_key, json.dumps(anchor or {}), json.dumps(meta or {}), time.time()))
    conn.commit()
    conn.close()
    return sid


def list_segments(job_id) -> list:
    conn = connect()
    rows = conn.execute("SELECT * FROM segments WHERE job_id=? ORDER BY order_index", (job_id,)).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["anchor"] = json.loads(d["anchor"] or "{}")
        d["meta"] = json.loads(d["meta"] or "{}")
        out.append(d)
    return out


def link_step_asset(workflow_id, order_index, asset_id):
    conn = connect()
    conn.execute("UPDATE steps SET asset_id=? WHERE workflow_id=? AND order_index=?",
                 (asset_id, workflow_id, order_index))
    conn.commit()
    conn.close()


def set_workflow_status(wf_key, status):
    conn = connect()
    conn.execute("UPDATE workflows SET status=? WHERE wf_key=?", (status, wf_key))
    conn.commit()
    conn.close()
