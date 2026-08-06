# 🛰 Spacebot

An internal knowledge assistant your documents never leave. Point it at what your team has
already written — runbooks, policies, spreadsheets, CVs, transcripts — and people ask
questions in plain language, getting **grounded, cited** answers or an honest "we don't have
that" that becomes a task for whoever does.

Runs **100% on your own hardware** against **open-source models** via Ollama. No cloud, no
API key, no Docker, no Postgres. Everything except the model is Python standard library +
SQLite, so "on premises" means one folder and one file.

---

## Use it with your own documents

```bash
# 1. local models (once) — generation + embeddings, both open source
curl -fsSL https://ollama.com/install.sh | sh     # or see "Install without sudo" below
ollama serve &
ollama pull llama3.2:3b        # generation
ollama pull nomic-embed-text   # embeddings, Apache-2.0, ~270MB

# 2. document parsers
pip install --user pypdf python-docx openpyxl beautifulsoup4 lxml numpy pillow

# 3. your install — names it, and creates one admin account
python3 setup.py --org "Your Company" --admin you@yourcompany.com

# 4. your documents
python3 ingest.py ./your-documents --publish

python3 server.py        # http://localhost:8080
```

`setup.py` prints a generated admin password once, or takes `--password`. The knowledge base
starts empty; `ingest.py` takes a file or a folder, skips anything already ingested, and
refuses files it cannot read rather than inventing content for them. Nothing in either step
reaches the network — parsing is local, and with Ollama configured, so is every model call.

### Or just look at it first

```bash
python3 seed.py          # DEMO data: a fictional company, published passwords
python3 server.py
```

`seed.py` is a demo, not a starting point — it loads an invented company's workflows and
logins whose passwords are in the file. Use `setup.py` for anything real.

Supported uploads: **PDF, DOCX, XLSX, CSV, HTML, images, text and transcripts.** Scanned
documents need OCR and media needs transcription — both are optional extras
(`apt-get install tesseract-ocr ffmpeg`, `pip install --user pytesseract`). Without them
those files report "awaiting capability" honestly rather than producing nothing.

Sign in with one of the demo accounts on the login page (one click each):

| Account | Sees |
|---|---|
| `raj@spacelabs.dev` / `raj123` | Chat only — the end-user experience |
| `sarah@spacelabs.dev` / `sarah123` | Chat + Knowledge Studio |
| `admin@spacelabs.dev` / `admin123` | Also Model settings |

Spacebot finds Ollama by itself. **If no model is reachable it still runs**, falling back to
an offline heuristic — the whole pipeline works, the answers are just stitched from stored
text instead of written fresh, and the UI says so rather than pretending.

### Install without sudo

The official installer needs root. If you don't have it, unpack the release into your home
directory instead — everything runs as your user:

```bash
curl -fL "$(curl -s https://api.github.com/repos/ollama/ollama/releases/latest \
  | grep -o 'https://[^"]*ollama-linux-amd64\.tar\.zst')" -o ollama.tar.zst
python3 -c "
import sys; from compression.zstd import ZstdFile      # Python 3.14+
with ZstdFile('ollama.tar.zst') as f:
    while (c := f.read(1<<20)): sys.stdout.buffer.write(c)" | tar -x -C ~/.local
~/.local/bin/ollama serve &
~/.local/bin/ollama pull llama3.2:3b
```

### Which model

`llama3.2:3b` is the default because it's the sweet spot on a CPU-only box: ~12 tokens/sec
on 12 cores, which streams at about reading speed. A 7B model follows the format rules
better but halves that, which is noticeable in a live demo. Any Ollama model works — pick
one on the **Model settings** page and Spacebot lists what you have installed.

---

## What to show in a demo

| Ask this | Shows |
|---|---|
| `the rollback failed with ERR_LEASE_HELD` | **Error-code routing** → exact workflow + the known-error fix, first |
| `how do I roll back a deploy?` | **Disambiguation** → prod vs staging, alternatives offered as chips |
| `what do I need to do in my first week?` | **Journey** → spans three onboarding workflows, in order |
| `how do I reset my office badge?` | **Abstention** → refuses to guess, logs a knowledge gap |
| then: `what about staging?` | **Memory** → resolves the follow-up against the conversation |
| Knowledge Studio | Paste a runbook → it becomes a live, askable workflow |
| Model settings | Swap the local model, or bring your own Claude/OpenAI key |

Every answer carries the source workflow, who verified it and when, and a confidence band —
the anti-hallucination receipts, in the UI chrome rather than in the model's prose.

---

## How it works

```
question ─► CONDENSE follow-up against the conversation ("what about staging?")
         ─► RETRIEVE  hybrid: dense embeddings + BM25, over chunks from every workflow
         ─► FUSE      reciprocal-rank fusion, then one hop across the relation graph
         ─► DECIDE    an answering policy from a measured evidence score
         ─► GENERATE  stream a natural answer in the shape the question calls for
```

Retrieval works on **chunks**, not whole workflows, so one answer can legitimately combine
a spreadsheet row, a step from a runbook and a paragraph from a PDF. Workflows are the unit
of authoring and permission; they are metadata on a chunk, not a wall around it.

There is **no failure message** in the answering path. Retrieval quality changes *how* we
answer, never *whether* we do:

| Evidence | Behaviour |
|---|---|
| strong | Answer it fully. |
| partial | Answer what's there, name the gap plainly, offer what we do have. |
| nothing | Warm one-liner, offer the nearest topic, or ask one clarifying question. |

Four things keep it honest:

- **An unreadable file stops the pipeline.** It never becomes content. A missing PDF parser
  once had its own error string structured into a fully invented, published workflow.
- **Thin material is never structured.** Under 200 characters, ingestion fails rather than
  asking a model to expand a sentence into a procedure.
- **The source format is the target format.** Verification lines and pitfalls reach the
  model already wearing their output markers, because a small model copies far more
  reliably than it follows a rule about what to write.
- **Provenance never comes from the model.** The prompt forbids naming people and forbids
  internal IDs; the UI renders both from the record.

Full detail, including the measured retrieval numbers and the path to millions of
documents, is in [`docs/ARCHITECTURE-RAG.md`](docs/ARCHITECTURE-RAG.md).

## Layout

```
spacebot/
├─ server.py          HTTP only: route table, auth, SSE framing. ~300 lines.
├─ ask.py             CLI ask (structured, graded output)
├─ seed.py            demo workflows + logins
├─ sb/
│  ├─ config.py       paths, thresholds, per-provider default models
│  ├─ settings.py     DB > env > default resolution; local-first `auto`
│  ├─ db.py           SQLite store (swap for Postgres+pgvector here, and only here)
│  ├─ providers.py    Ollama / Anthropic / OpenAI / offline, behind one interface
│  ├─ prompts.py      versioned prompts (grounding + citation rules)
│  ├─ render.py       workflow → prompt text, and answer → markdown
│  ├─ pipeline.py     condense → route → retrieve → compose → ground & gate
│  ├─ ingest.py       files/text → structured workflow
│  ├─ media/          blob store, source adapters, capability seams
│  └─ web/            the entire UI — index/login, app.css, app.js, markdown.js
└─ data/spacebot.db   created on first run (delete to reset)
```

The server knows nothing about the UI and the UI knows nothing about the model. Adding an
endpoint is one entry in `ROUTES`; adding a provider is one subclass; changing the look is
a CSS file.

## Not in the POC (deliberately)

Video transcription and frame extraction (the adapters and capability seams exist and
report "awaiting capability" honestly), embeddings/vector search (not needed at this corpus
size — scoping does the work), multi-tenant isolation, presigned uploads. All are documented
in `../SPACEBOT.md` as the path from POC → product.
