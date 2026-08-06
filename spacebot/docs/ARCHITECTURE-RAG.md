# Spacebot RAG architecture

Everything here runs locally on open-source models. No API key, no network egress, no
managed service. The whole retrieval stack is Python standard library plus numpy; the
models are served by Ollama.

| Concern | Choice | Why |
|---|---|---|
| Generation | `llama3.2:3b` via Ollama | ~12 tok/s on 12 CPU cores — streams at reading speed. Any Ollama model works. |
| Embeddings | `nomic-embed-text` (Apache-2.0, 768-dim) | ~270 MB, batched, embeds a document in well under a second on CPU. |
| Lexical | BM25, hand-rolled | No dependency, exact on rare literals, completely reproducible. |
| Vector store | SQLite BLOB + numpy dot product | Exact search, no index build, no service. Swap point documented below. |

---

## Pipeline

```
UPLOAD ──► adapters ──► segments ──► structure ──► workflow draft ──► PUBLISH
                                          │                              │
                                          └── source text kept ──────────┤
                                                                         ▼
                                                                   chunk + embed
                                                                         │
ASK ──► condense ──► hybrid retrieve ──► fuse ──► expand ──► policy ──► generate
                     (dense + BM25)      (RRF)   (relations)
```

### 1. Ingestion — `sb/media/`

One adapter per file type, each emitting the same `Segment` shape, so nothing downstream
knows what a file was. Adding a format is one class in `adapters.py`.

| Type | Adapter | Notes |
|---|---|---|
| PDF | `PdfAdapter` (pypdf) | Page text + embedded images. Scanned PDFs route to OCR. |
| DOCX | `DocxAdapter` (python-docx) | Headings preserved; tables flattened to rows. |
| XLSX / CSV | `SheetAdapter` / `CsvAdapter` (openpyxl) | **One segment per row**, header repeated into each. |
| HTML | `HtmlAdapter` (bs4) | Scripts/nav/footer stripped. |
| Images | `ImageAdapter` | OCR first (exact), then a vision model (descriptive). |
| Audio / video | `AudioAdapter` / `VideoAdapter` | Wired to a `Transcriber` / `FrameExtractor` seam. |
| Text / transcripts | `TranscriptAdapter` | Paragraph-aware. |

Two invariants earned the hard way:

**A missing capability must never become content.** An adapter that can't read its input
*raises*. It used to return `"[PDF parsing needs pypdf]"` as if that string were the
document, and the structuring model turned it into a complete, plausible, published
workflow about parsing PDFs — fabricated commands, fabricated error codes, answered at
high confidence. Unreadable input stops the pipeline.

**Thin material must never be structured.** Under `MIN_MATERIAL_CHARS` (200) with no
images, the job fails instead of asking a model to expand a sentence into a procedure.

The extracted source text is attached to the workflow as an asset. Without that, the only
record of an upload is the model's structured summary of it, and anything the structurer
didn't think to capture becomes permanently unanswerable.

### 2. Chunking — `sb/chunks.py`

Workflows are the unit of *authoring* and *permission*. Chunks are the unit of *retrieval*.
Conflating them was the previous design's ceiling: one workflow was the smallest fetchable
thing, so one workflow was the most any answer could use.

Chunks are ~1200 characters with 180 overlap, split on paragraph then sentence boundaries,
never mid-word. Each carries its workflow name and section heading into the embedded text,
so a bare `Run \`make bootstrap\`` still retrieves and still reads sensibly in a citation.

Per workflow we emit: summary, one chunk per step, one per known error, one per FAQ, and
the source document split.

Step chunks are written with their success check already marked (`✓ …`) and pitfalls as
`Heads up: …`. The retrieved format *is* the target output format — a small model copies
what it sees far more reliably than it follows a rule about what to write.

### 3. Retrieval — `sb/retrieval.py`

**Hybrid, because each leg covers the other's failure.** Dense generalises ("undo a bad
release" → rollback runbook) but blurs rare literals. Enterprise questions are full of rare
literals: `ERR_LEASE_HELD`, `spacectl`, `JNTUK`, a person's name. BM25 is exact on those and
hopeless at paraphrase.

Measured on this corpus:

```
                dense (cosine)   bm25        cross-retriever overlap
answerable      0.642 – 0.862    3.2 – 12.1  2 – 8
not answerable  0.424 – 0.651    0.00        0
```

Dense alone **cannot** separate them — the ranges overlap at 0.64/0.65. Anything gating on
cosine alone will answer questions about the office wifi password out of a deploy runbook.
BM25 presence is the real discriminator, which is why it carries the most weight in the
evidence score.

**Fusion is Reciprocal Rank Fusion** — `Σ weight / (k + rank)`, k=60. It combines by rank,
not score, because a cosine and a BM25 value are not on comparable scales and normalising
them into one number is a fudge that needs retuning whenever the corpus changes.

**Cross-workflow by construction.** Chunks are ranked globally, then grouped by workflow —
so an answer combining a spreadsheet row with a Word policy is the normal path, not a
special case. After grouping, the workflow relation graph is walked **one hop**: a purchase
order that names vendor approval as a prerequisite pulls that context in even when the
question never mentioned it. One hop, not transitive closure — relevance decays faster than
the extra context helps.

**Context ordering matters more than it looks.** Within a workflow, facts are ordered by
relevance and steps by document order. Ordering everything by document order buried the
best chunk under its near-misses: asked who was on call in week W32, the model read W31's
row first — same columns, adjacent in the sheet — and answered from that. Best evidence
goes at the top of the window.

### 3a. Subject attribution — `sb/subjects.py`

**Every chunk records whose knowledge it is.** Without this, retrieval can only tell that a
document *looks like* an answer, not that it is about the right person — and a knowledge
base holding several people answers "any info about Sreedhar Masula?" out of Yaswanth's CV,
because a CV is exactly the shape of thing a question about a person should match.

Subjects are extracted at index time from three signals, each earned by a measured failure:

| Signal | Without it |
|---|---|
| Capitalisation ratio, measured **corpus-wide** | Per-document, every doc's own topic words look like names: `policy`, `week`, `status` |
| **Position** — sentence-initial capitals ignored | Imperative runbook prose makes `roll`, `revert`, `confirm`, `get` the "names" of half the corpus |
| **Common-word list**, single tokens only | `create` becomes a subject, and "how do I create a local dev environment" gates onto the purchase-order workflow |

Multi-word capitalised runs are exempt from the word list, so names built from ordinary
words — *Modern Signal*, *Acme Ltd* — survive. Possessives count as name evidence even
sentence-initially, which is the only thing in the corpus identifying *Meena* as a person.

Subjects then do three jobs, and deliberately **not** a fourth:

- **Rank.** A question naming someone promotes chunks about them (`SUBJECT_BOOST = 0.35`)
  so the right person's excerpts reach the top of the model's context.
- **Abstain.** Two conditions end the answer rather than shape it: `unknown_subjects` (the
  question names a person or company the corpus has never mentioned) and `subject_miss`
  (it names someone we do hold, but nothing retrieved is about them).
- **Label.** Each workflow's excerpts arrive prefixed `These excerpts are about: …`, so the
  boundary is visible when one answer legitimately spans several subjects.
- **Never filter.** Extraction is a heuristic; a heuristic allowed to silently delete
  evidence turns a wrong answer into a missing one, which is worse because nobody reports
  it. A bad subject can reorder, never suppress.

Keeping that last promise needed a structural split, not a smaller constant. Two mechanisms
quietly turn a boost into a filter: the relative cutoff is measured from the top score, so
promoting the winner raises the floor under everything else, and the chunk quota is a hard
count, so a promoted document fills it first. Measured on *"is Yaswanth on call this week"*,
the boost took the on-call rota from three chunks to zero and left only his CV — deleting
the one document that could answer, in the name of finding the right person. So **admission
uses the unboosted ranking and ordering uses the boosted one**: which chunks are retrieved
is exactly what it was before subjects existed, and the boost only decides what the model
reads first.

**Corpus-absence alone is not evidence of an unknown entity**, and measuring proved it:
`call`, `32`, `limit` and `expenses` are all missing from the lexical index — artefacts of
tokenising `on-call` whole and of the corpus never writing "expenses" unpluralised — yet
every question containing them answers correctly. So absence only counts when the word also
looks like a name: two or more adjacent unknown words, a possessive, or an explicit frame
("about X", "a vendor called X"). Ordinary English never qualifies.

Gating subjects are a stricter set than labelling subjects — additionally filtered to
exclude ordinary words and names shared by more than half the corpus (`SpaceLabs` is a real
name and filters nothing).

### 4. Answering policy — `sb/rag.py`

Retrieval quality changes *how* we answer, never *whether* we do. There is no failure
message anywhere in this path.

| Evidence | Policy | Behaviour |
|---|---|---|
| ≥ 0.40 | `answer` | Answer it fully. |
| 0.15 – 0.40 | `partial` | Answer what's there, name the gap plainly, offer what we do have. |
| < 0.15 | `nothing` | Warm one-liner, offer the nearest topic, or ask one clarifying question. |

Three conditions override the score straight to `nothing`, all variations on "the material
is about somebody else": `unsupported_terms` (nothing in the question appears in the
retrieved text), `unknown_subjects`, and `subject_miss`. The override exists because the
score alone was not enough — the Sreedhar Masula question scores **≈0.41**, a hair over the
answer threshold, and was answered confidently out of the wrong CV. No threshold tuning
fixes that: the score is a fair reading of how well the retrieved text matches, and the
retrieved text genuinely does match. It is simply about someone else.

When one fires, the context is **removed**, not annotated. Keeping the excerpts and telling
the model not to use them does not work: handed a page of CV facts and asked about
availability, it either recited them or invented a negative ("is not available at any
specific timings"). You cannot reliably out-prompt a full context window. The model is
instead given the names asked about and the nearest topics we do hold, which is what turns
a dead end into "I don't have anything on Sreedhar Masula, though I do have Yaswanth's CV".

Thresholds sit inside a wide measured margin: every answerable probe scored ≥ 0.53, every
unanswerable one ≤ 0.08.

The interesting case is "what is Yaswanth's blood group" — we hold a document about the
person but not that fact. It lands mid-band deliberately: the right answer is to retrieve
the CV and say what it does and doesn't cover, not to abstain and not to invent.

**Answer shape is decided in code, not by the model.** A 3B model handed three shape options
picks wrong often enough to matter — the same question came back as a numbered list once and
a wall of prose the next time. The pipeline already knows whether it retrieved ordered steps,
so it decides and hands the model one unambiguous directive including the exact step count.
For procedural questions it also fetches the *complete* step set, because an answer missing
step 4 because step 4 ranked below the cut is worse than useless.

### 5. Post-processing — `sb/postprocess.py`

Deterministic transforms between model and user: drop a bolted-on title line (models want
to head answers with the document name, which reads like a report), and replace internal
IDs like `PROC.VENDOR_APPROVAL` with human names.

---

## Scaling to millions of documents

Current implementation is exact brute force: every vector in one float32 matrix, one numpy
dot product per query. On this corpus that is ~40 ms end to end. It stays viable to roughly
**100k chunks** (~1 GB RAM, sub-200 ms).

`chunks.search_dense()` is the **only** function that changes. Its contract —
`(query, limit) -> [{chunk_id, score}]` — is what every caller depends on.

| Scale | Change | Everything else |
|---|---|---|
| < 100k chunks | As-is | — |
| 100k – 10M | Swap the matrix scan for FAISS `IndexIVFFlat` or `hnswlib`, load at boot | unchanged |
| 10M+ / multi-node | Qdrant or pgvector behind the same signature; move `chunks` to Postgres | unchanged |

Other work that becomes necessary, in the order it starts to hurt:

1. **Embed out of process.** Embedding is inline on publish today. Past a few hundred
   documents per hour it belongs on a queue; the `chunks.vector IS NULL` column is already
   the work list, and `embed_pending()` is already incremental and idempotent.
2. **BM25 → a real inverted index.** The in-process index is rebuilt from a full table scan
   on a 20-second TTL. Move to SQLite FTS5 (one statement) or OpenSearch.
3. **Pre-filter by permission before scoring.** `workflow_id` is already on every chunk;
   a WHERE clause becomes a metadata filter in the vector store.
4. **A reranker.** A cross-encoder over the top ~50 fused results buys the largest single
   quality jump once the corpus is big enough that top-10 recall degrades. `bge-reranker-base`
   runs locally.
5. **Cache embeddings by content hash.** Re-ingesting an unchanged document currently
   re-embeds it.

None of these change the pipeline, the prompts, or the UI.

---

## Running it

```bash
ollama serve &
ollama pull llama3.2:3b
ollama pull nomic-embed-text
pip install --user pypdf python-docx openpyxl beautifulsoup4 lxml numpy pillow

python3 seed.py
python3 server.py        # http://localhost:8080
```

Optional, for scanned documents and media:

```bash
apt-get install tesseract-ocr ffmpeg     # needs root
pip install --user pytesseract
```

Without them, scanned PDFs and video report `awaiting capability` honestly rather than
silently producing nothing.

**Index management** (admin): `GET /api/index/status` shows chunk and embedding counts and
embedder health; `POST /api/index/rebuild` re-chunks and re-embeds everything. Publishing a
workflow indexes it automatically.
