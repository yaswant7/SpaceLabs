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


def connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(config.DB_PATH), exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
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
    conn.commit()
    conn.close()


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
    row = cur.execute("SELECT id FROM workflows WHERE wf_key=?", (wf["wf_key"],)).fetchone()
    wid = row["id"] if row else _id()
    if row:
        cur.execute("DELETE FROM steps WHERE workflow_id=?", (wid,))
        cur.execute("DELETE FROM known_errors WHERE workflow_id=?", (wid,))
        cur.execute("DELETE FROM faqs WHERE workflow_id=?", (wid,))
    cur.execute(
        """INSERT INTO workflows(id,wf_key,name,summary,category,owner,status,version,
               trigger_phrases,verified_by,verified_at,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET
               name=excluded.name, summary=excluded.summary, category=excluded.category,
               owner=excluded.owner, status=excluded.status, version=version+1,
               trigger_phrases=excluded.trigger_phrases,
               verified_by=excluded.verified_by, verified_at=excluded.verified_at""",
        (wid, wf["wf_key"], wf["name"], wf.get("summary", ""), wf.get("category", ""),
         wf.get("owner", ""), wf.get("status", "published"), 1,
         json.dumps(wf.get("trigger_phrases", [])),
         wf.get("verified_by", ""), wf.get("verified_at", ""), time.time()),
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
def get_catalog() -> list:
    """Compact cards for the router — one row per workflow, cheap to put in a prompt."""
    conn = connect()
    rows = conn.execute(
        "SELECT id,wf_key,name,summary,category,owner,trigger_phrases FROM workflows WHERE status='published'"
    ).fetchall()
    conn.close()
    cards = []
    for r in rows:
        cards.append({
            "id": r["id"], "wf_key": r["wf_key"], "name": r["name"],
            "summary": r["summary"], "category": r["category"], "owner": r["owner"],
            "trigger_phrases": json.loads(r["trigger_phrases"] or "[]"),
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
        "steps": [step_dict(s) for s in steps],
        "extra_assets": [{"kind": a["kind"], "filename": a["filename"], "text": a["text"]}
                         for a in assets.values() if a["id"] not in {s["asset_id"] for s in steps}],
        "known_errors": [{"code": e["code"], "cause": e["cause"], "resolution": e["resolution"]} for e in kes],
        "faqs": [{"question": f["question"], "answer": f["answer"]} for f in faqs],
        "related": [{"kind": r["kind"], "wf_key": r["wf_key"], "name": r["name"]} for r in rels],
    }


def list_workflows() -> list:
    conn = connect()
    rows = conn.execute(
        "SELECT w.id,w.wf_key,w.name,w.category,w.owner,w.verified_by,w.status, "
        "(SELECT COUNT(*) FROM steps s WHERE s.workflow_id=w.id) AS step_count, "
        "(SELECT COUNT(*) FROM assets a WHERE a.workflow_id=w.id) AS asset_count "
        "FROM workflows w ORDER BY w.category, w.name").fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---- learning loop --------------------------------------------------------
def log_ask(question, route, workflow_ids, answer, confidence, abstained, provider):
    conn = connect()
    conn.execute(
        "INSERT INTO ask_log(id,question,route,workflow_ids,answer,confidence,abstained,provider,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (_id(), question, json.dumps(route), json.dumps(workflow_ids), json.dumps(answer),
         confidence, 1 if abstained else 0, provider, time.time()))
    conn.commit()
    conn.close()


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
