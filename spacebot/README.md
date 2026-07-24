# 🛰 Spacebot — POC

A workflow-scoped knowledge assistant for the SpaceLabs team. Seniors feed knowledge in
(keyed by Workflow ID); new members ask questions in plain language and get **grounded,
cited** answers — or an honest "I don't know" that becomes a task for a senior.

Runs **100% locally** with **zero paid dependencies**. No Docker, no Postgres, no cloud.
Everything except the optional model call is Python standard library + SQLite.

---

## Run it (30 seconds)

```bash
cd spacebot
python3 seed.py            # load 7 realistic SpaceLabs workflows
python3 server.py          # open http://localhost:8080
```

Or from the terminal:

```bash
python3 ask.py "rollback failed ERR_LEASE_HELD what do I do"
python3 ask.py "what do I do in my first week"
python3 ask.py "how do I reset my office badge"     # -> honest abstain
```

With **no API key** it runs in **mock mode** (offline heuristic) — the full pipeline works,
answers are simpler. Add a key for real quality (see below).

---

## What to show in a demo

| Ask this | Shows |
|---|---|
| `rollback failed ERR_LEASE_HELD` | **Error-code routing** → exact workflow + the known-error fix, cited |
| `how do I roll back a deploy` | **Disambiguation** → prod vs staging, alternatives offered |
| `what do I do in my first week` | **Journey** → spans 3 onboarding workflows in order |
| `how do I reset my office badge` | **Abstention** → refuses to guess, logs a knowledge gap |
| **Feed knowledge** page | Paste a runbook → it becomes a live, askable workflow |
| **Model settings** page | Swap Claude ↔ OpenAI ↔ local, bring your own key |

Every answer carries: the source workflow, who verified it and when, a confidence band,
and **citation coverage** — the anti-hallucination receipts.

---

## Use a real model

Either drop a key in `.env` (copy `.env.example`), **or** just open **`/admin`** and paste it —
pick Anthropic (Claude), OpenAI, or any OpenAI-compatible endpoint (Azure, local Ollama via
`OPENAI_BASE_URL`). Stored locally in SQLite. No restart needed.

```bash
cp .env.example .env        # then edit ANTHROPIC_API_KEY=...
python3 server.py
```

---

## How it works (the pipeline)

```
question ─► ROUTE (top-K workflows + confidence)
             ├─ one clear winner   → scoped answer from that workflow only
             ├─ two close matches  → disambiguation chips
             ├─ spans several      → journey across workflows
             └─ nothing fits       → abstain + log a knowledge gap
          ─► RETRIEVE the chosen workflow package(s) — nothing else can leak in
          ─► COMPOSE a structured, cited answer
          ─► GRADE: citation coverage + confidence gate (weak → abstain, never bluff)
```

Everything is scoped by `workflow_id`, so a rollback question can never pull in a
procurement doc. That containment is the main hallucination defense.

## Layout

```
spacebot/
├─ server.py          stdlib web server (Ask / Feed / Admin)
├─ ask.py             CLI ask
├─ seed.py            demo workflows
├─ sb/
│  ├─ config.py       paths, thresholds, per-provider default models
│  ├─ settings.py     DB > env > default resolution (bring-your-own-model)
│  ├─ db.py           SQLite store (swap for Postgres+pgvector later — this file only)
│  ├─ providers.py    Anthropic / OpenAI / Mock behind one interface
│  ├─ prompts.py      versioned prompts (grounding + citation rules)
│  ├─ pipeline.py     route → retrieve → compose → ground & gate
│  └─ ingest.py       files/text → structured workflow (PDF via pypdf, images via vision)
└─ data/spacebot.db   created on first run (delete to reset)
```

## Not in the POC (deliberately)

Browser file-upload UI, video/transcription, embeddings/vector search (not needed at this
corpus size — scoping does the work), auth, multi-tenant. All are documented in
`../SPACEBOT.md` as the path from POC → product.
