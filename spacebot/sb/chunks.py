"""Chunk store — the unit of retrieval.

Workflows are the unit of *authoring* and *permission*; chunks are the unit of *retrieval*.
Conflating the two was the old design's ceiling: it could only ever answer from one
workflow, because one workflow was the smallest thing it could fetch. Retrieving chunks
instead means an answer can combine a step from a deploy runbook with a fact from a CV
with a row from a spreadsheet, and the workflow becomes metadata carried on the chunk
rather than a wall around it.

Storage is SQLite with float32 BLOBs and brute-force cosine. That is the right choice up
to roughly 10^5 chunks on this hardware: one numpy dot product over the whole corpus,
no index to build, no service to run, exact results. Past that, `search_dense` is the
only function that changes — swap the matrix scan for FAISS/pgvector/Qdrant and every
caller stays as it is. The scaling notes are in `docs/ARCHITECTURE-RAG.md`.
"""
import json
import re
import sqlite3
import time
import uuid

from . import config, db, embed

try:
    import numpy as np
except ImportError:
    np = None


SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
    id TEXT PRIMARY KEY,
    workflow_id TEXT,
    wf_key TEXT,
    source TEXT,               -- step-3 | faq-2 | error | summary | document:<file>
    ordinal INTEGER,           -- position within the workflow, for stitching context
    heading TEXT,              -- human label shown in citations
    text TEXT,
    tokens INTEGER,
    vector BLOB,               -- float32, L2-normalised; NULL until embedded
    model TEXT,                -- which embedder produced `vector`
    meta TEXT DEFAULT '{}',
    -- The rest of the chain: source → segment → chunk. Without these a citation can name
    -- the document but not the place in it, which is the difference between "from the CV"
    -- and "page 2, paragraph 14".
    source_id TEXT,            -- the uploaded file this text came from
    segment_id TEXT,           -- the parsed unit within it (page, paragraph, row, scene)
    anchor TEXT DEFAULT '{}',  -- {page:N} | {para:N} | {sheet,row} | {t_start,t_end}
    created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_chunk_wf ON chunks(workflow_id);
CREATE INDEX IF NOT EXISTS idx_chunk_key ON chunks(wf_key);
CREATE INDEX IF NOT EXISTS idx_chunk_pending ON chunks(model) WHERE vector IS NULL;
"""


def init():
    conn = db.connect()
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


def _id():
    return uuid.uuid4().hex[:16]


# ---------------------------------------------------------------- chunking --
_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])")


def split_text(text: str, size: int = None, overlap: int = None) -> list:
    """Split on paragraph boundaries, then sentences, never mid-word.

    Respecting structure matters more than hitting an exact length: a chunk that ends
    halfway through a command is a chunk that can never answer a question about that
    command, however good the embedding model is.
    """
    size = size or config.CHUNK_CHARS
    overlap = overlap or config.CHUNK_OVERLAP
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]

    # paragraphs first
    units, buf = [], []
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if len(para) <= size:
            units.append(para)
        else:
            # too big: fall back to sentences, then to a hard cut
            cur = ""
            for sent in _SENT.split(para):
                if len(cur) + len(sent) + 1 <= size:
                    cur = f"{cur} {sent}".strip()
                else:
                    if cur:
                        units.append(cur)
                    while len(sent) > size:
                        cut = sent.rfind(" ", 0, size)
                        cut = cut if cut > size * 0.6 else size
                        units.append(sent[:cut].strip())
                        sent = sent[cut:].strip()
                    cur = sent
            if cur:
                units.append(cur)

    # pack units up to `size`, carrying `overlap` characters between chunks
    chunks = []
    for unit in units:
        if not buf:
            buf = [unit]
            continue
        if sum(len(b) for b in buf) + len(unit) + 2 <= size:
            buf.append(unit)
        else:
            chunks.append("\n\n".join(buf))
            tail = chunks[-1][-overlap:] if overlap else ""
            tail = tail[tail.find(" ") + 1:] if " " in tail else ""
            buf = [tail, unit] if tail else [unit]
    if buf:
        chunks.append("\n\n".join(buf))

    # merge a runt tail into its predecessor
    if len(chunks) > 1 and len(chunks[-1]) < config.CHUNK_MIN:
        chunks[-2] = chunks[-2] + "\n\n" + chunks.pop()
    return [c.strip() for c in chunks if c.strip()]


def package_to_chunks(pkg: dict) -> list:
    """Turn one workflow package into retrievable chunks.

    Each chunk carries enough heading context to stand alone — a bare step body like
    "Run `make bootstrap`" retrieves poorly and reads worse in a citation, so the workflow
    name and step title are prepended into the embedded text itself.
    """
    key, name = pkg["wf_key"], pkg.get("name", "")
    out = []

    summary = (pkg.get("summary") or "").strip()
    if summary:
        out.append({"source": "summary", "heading": name,
                    "text": f"{name}\n{summary}"})

    for s in pkg.get("steps") or []:
        # Success checks and pitfalls are written into the chunk already wearing their
        # output markers. The composer copies what it sees far more reliably than it
        # follows a rule about what to write, so the retrieved format IS the target format.
        title = s.get("title") or f"Step {s.get('order')}"
        lines = [f"{name} — step {s.get('order')}: {title}", (s.get("body") or "").strip()]
        if s.get("verification"):
            lines.append(f"✓ {s['verification'].strip()}")
        for m in s.get("mistakes") or []:
            lines.append(f"Heads up: {m}")
        for t in s.get("tips") or []:
            lines.append(f"Tip: {t}")
        text = "\n".join(x for x in lines if x)
        out.append({"source": f"step-{s.get('order')}", "heading": title, "text": text})

    for i, e in enumerate(pkg.get("known_errors") or [], 1):
        text = (f"{name} — error {e.get('code','')}\n"
                f"Cause: {e.get('cause','')}\nFix: {e.get('resolution','')}")
        out.append({"source": f"error-{i}", "heading": e.get("code", "Known error"),
                    "text": text})

    for i, f in enumerate(pkg.get("faqs") or [], 1):
        text = f"{name}\nQ: {f.get('question','')}\nA: {f.get('answer','')}"
        out.append({"source": f"faq-{i}", "heading": f.get("question", "FAQ")[:80],
                    "text": text})

    # The source document. This is what makes an uploaded file genuinely searchable rather
    # than only its model-written summary.
    #
    # Split on the document's OWN boundaries when the adapters recovered them — a heading
    # starts a new chunk, a table row is a chunk, an HTML section is a chunk — and fall back
    # to character-count splitting only for material that arrived as undifferentiated prose.
    # Chunking a table on character count cuts rows in half; chunking it on rows means a
    # question about one row retrieves that row.
    for a in pkg.get("extra_assets") or []:
        fname = a.get("filename") or "document"
        structure = a.get("structure")

        if structure:
            j = 0
            for seg in structure:
                body = (seg.get("text") or "").strip()
                if not body:
                    continue
                # Anything the ingest pipeline flagged as holding a credential stays out of
                # the index entirely. It was detected and then ignored: the flag was written
                # onto the segment and dropped at chunking, so screenshots of tokens were
                # being embedded and served like any other content.
                if seg.get("secret"):
                    continue
                kind = seg.get("kind") or "prose"
                # A table row is already the right size; only prose needs splitting.
                pieces = [body] if kind in ("table", "table_row", "sheet") else split_text(body)
                for piece in pieces:
                    j += 1
                    out.append({"source": f"document:{fname}#{j}", "heading": fname,
                                "text": f"{name} — {fname}\n{piece}",
                                "meta": {"kind": kind, "file": fname},
                                # Splitting one segment into several pieces keeps them all
                                # pointing at that segment: the anchor is the segment's
                                # location, and every piece genuinely came from there.
                                "source_id": a.get("source_id") or "",
                                "segment_id": seg.get("segment_id") or "",
                                "anchor": seg.get("anchor") or {}})
        else:
            body = (a.get("text") or "").strip()
            if not body:
                continue
            for j, piece in enumerate(split_text(body), 1):
                out.append({"source": f"document:{fname}#{j}", "heading": fname,
                            "text": f"{name} — {fname}\n{piece}",
                            "meta": {"kind": "prose", "file": fname}})

    for i, c in enumerate(out):
        c.update({"wf_key": key, "ordinal": i, "workflow_id": pkg["id"]})
    return out


# ------------------------------------------------------------- persistence --
def reindex_workflow(workflow_id: str, pkg: dict = None) -> int:
    """Rebuild every chunk for one workflow. Called after authoring or ingestion."""
    init()
    pkg = pkg or db.get_package(workflow_id)
    if not pkg:
        return 0
    rows = package_to_chunks(pkg)

    conn = db.connect()
    conn.execute("DELETE FROM chunks WHERE workflow_id=?", (workflow_id,))
    now = time.time()
    conn.executemany(
        "INSERT INTO chunks(id,workflow_id,wf_key,source,ordinal,heading,text,tokens,"
        "vector,model,meta,source_id,segment_id,anchor,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(_id(), workflow_id, c["wf_key"], c["source"], c["ordinal"], c["heading"],
          c["text"], len(c["text"]) // 4, None, None,
          json.dumps(c.get("meta") or {}), c.get("source_id") or "",
          c.get("segment_id") or "", json.dumps(c.get("anchor") or {}), now)
         for c in rows])
    conn.commit()
    conn.close()
    return len(rows)


def reindex_all() -> int:
    total = 0
    for card in db.get_catalog():
        total += reindex_workflow(card["id"])
    return total


def pending_count() -> int:
    init()
    conn = db.connect()
    n = conn.execute("SELECT COUNT(*) c FROM chunks WHERE vector IS NULL").fetchone()["c"]
    conn.close()
    return n


def embed_pending(limit: int = 500) -> dict:
    """Embed chunks that don't have a vector yet.

    Runs after ingestion and on demand. Deliberately incremental and idempotent: if the
    embedder is down, chunks simply stay unembedded and retrieval falls back to BM25
    until it comes back — nothing is lost and nothing blocks.
    """
    init()
    conn = db.connect()
    rows = conn.execute(
        "SELECT id, text FROM chunks WHERE vector IS NULL LIMIT ?", (limit,)).fetchall()
    conn.close()
    if not rows:
        return {"embedded": 0, "pending": 0}

    embedder = embed.get_embedder()
    try:
        vecs = embedder.embed([r["text"] for r in rows])
    except embed.EmbeddingUnavailable as e:
        return {"embedded": 0, "pending": len(rows), "error": str(e)}

    conn = db.connect()
    conn.executemany(
        "UPDATE chunks SET vector=?, model=? WHERE id=?",
        [(embed.pack(embed.normalize(v)), embedder.name, r["id"])
         for r, v in zip(rows, vecs)])
    conn.commit()
    conn.close()
    return {"embedded": len(rows), "pending": pending_count(), "model": embedder.name}


# ------------------------------------------------------------------ search --
_matrix_cache = {"at": 0.0, "ids": None, "matrix": None, "count": 0}
_MATRIX_TTL = 20.0


def _vector_matrix():
    """Stack every stored vector once and reuse it. Rebuilt when the corpus changes.

    This is the part that gets replaced by a real vector index past ~100k chunks; until
    then a contiguous float32 matrix and one dot product is both simpler and faster than
    anything with a build step.
    """
    conn = db.connect()
    n = conn.execute("SELECT COUNT(*) c FROM chunks WHERE vector IS NOT NULL").fetchone()["c"]
    now = time.time()
    if (_matrix_cache["matrix"] is not None and _matrix_cache["count"] == n
            and now - _matrix_cache["at"] < _MATRIX_TTL):
        conn.close()
        return _matrix_cache["ids"], _matrix_cache["matrix"]

    rows = conn.execute(
        "SELECT id, vector FROM chunks WHERE vector IS NOT NULL").fetchall()
    conn.close()
    ids = [r["id"] for r in rows]
    if not ids:
        matrix = None
    elif np is not None:
        matrix = np.vstack([np.frombuffer(r["vector"], dtype="<f4") for r in rows])
    else:
        matrix = [embed.unpack(r["vector"]) for r in rows]
    _matrix_cache.update({"at": now, "ids": ids, "matrix": matrix, "count": n})
    return ids, matrix


def invalidate():
    _matrix_cache.update({"at": 0.0, "ids": None, "matrix": None, "count": -1})


def search_dense(query: str, limit: int = None) -> list:
    """Semantic search. Returns [{chunk_id, score}] best first, or [] if unavailable."""
    limit = limit or config.RETRIEVE_CANDIDATES
    ids, matrix = _vector_matrix()
    if not ids:
        return []
    try:
        qv = embed.normalize(embed.get_embedder().embed_one(query))
    except embed.EmbeddingUnavailable:
        return []          # lexical search carries the request on its own

    sims = embed.cosine_matrix(qv, matrix)
    if np is not None:
        top = np.argsort(sims)[::-1][:limit]
        return [{"chunk_id": ids[i], "score": float(sims[i])} for i in top]
    order = sorted(range(len(sims)), key=lambda i: sims[i], reverse=True)[:limit]
    return [{"chunk_id": ids[i], "score": float(sims[i])} for i in order]


def get_many(chunk_ids: list) -> dict:
    if not chunk_ids:
        return {}
    conn = db.connect()
    q = ",".join("?" * len(chunk_ids))
    rows = conn.execute(
        f"SELECT id,workflow_id,wf_key,source,ordinal,heading,text,meta,"
        f"source_id,segment_id,anchor FROM chunks WHERE id IN ({q})", chunk_ids).fetchall()
    conn.close()
    out = {}
    for r in rows:
        d = dict(r)
        for field in ("meta", "anchor"):
            try:
                d[field] = json.loads(d.get(field) or "{}")
            except (ValueError, TypeError):
                d[field] = {}
        out[r["id"]] = d
    return out


def steps_for(wf_key: str) -> list:
    """Every step chunk of one workflow, in order.

    Retrieval returns the best-matching chunks, but "how do I X?" needs the whole
    procedure — an answer missing step 4 because step 4 happened to score below the cut
    is worse than useless. Once we know a question is procedural we fetch the complete
    set rather than whatever ranked.
    """
    conn = db.connect()
    rows = conn.execute(
        "SELECT id,workflow_id,wf_key,source,ordinal,heading,text FROM chunks "
        "WHERE wf_key=? AND source LIKE 'step-%' ORDER BY ordinal", (wf_key,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def all_chunks() -> list:
    """Full corpus, for the lexical index."""
    init()
    conn = db.connect()
    rows = conn.execute(
        "SELECT id,workflow_id,wf_key,source,heading,text FROM chunks").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def stats() -> dict:
    init()
    conn = db.connect()
    r = conn.execute(
        "SELECT COUNT(*) total, SUM(vector IS NOT NULL) embedded, "
        "COUNT(DISTINCT workflow_id) workflows FROM chunks").fetchone()
    conn.close()
    return {"chunks": r["total"] or 0, "embedded": r["embedded"] or 0,
            "workflows": r["workflows"] or 0}
