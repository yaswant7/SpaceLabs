# Spacebot — End-to-End Architecture

**For:** the SpaceLabs project team
**What it is:** a single point of contact that answers new team members' "how do I…" questions from knowledge that senior engineers feed in, organized by workflow, with every answer showing its receipts.
**Status:** build spec, demo-first
**Codename:** Spacebot (changeable later)

---

## 0. The promise (say this to your seniors)

> **You explain each thing once. After that, Spacebot explains it — and shows exactly where the answer came from.**

That single sentence is the whole product. Everything below exists to make it true.

Two design commitments that follow from it, and that you flagged yourself:

1. **Workflow is the unit of everything.** Not documents, not folders. A senior dumps a PDF, three screenshots, and a 6-minute video, tells Spacebot *"this is for `DEPLOY.PROD_ROLLBACK`,"* and all of it becomes that one workflow's knowledge. When someone later asks about a prod rollback, Spacebot answers **only from that workflow's materials** — not from a soup of everything. This is why your "segregate by workflow" instinct is correct: it makes retrieval small, scoped, and precise, and it kills the biggest source of wrong answers (pulling a plausible-but-unrelated chunk from some other document).

2. **It can be confidently wrong — so it isn't allowed to be confident without receipts.** You're right that this is dangerous. A tool that guides new hires and lies is worse than no tool. The defense is not "use a better model." The defense is architectural: every answer is grounded in a specific step / page / timestamp, and when Spacebot doesn't have the material, **it says so and pings the owner instead of guessing.** Section 7 is entirely about this.

---

## 1. The two people and the two loops

Spacebot has exactly two kinds of users and two loops. Keep this in your head; the whole system is just these two loops turning.

```
        FEED LOOP (seniors)                      ASK LOOP (new members)
        ───────────────────                      ──────────────────────
  Senior picks / creates a Workflow ID     New member asks in plain language
            │                                          │
  Drops PDF / PNG / video / transcript      Spacebot finds the workflow
            │                                          │
  Spacebot auto-structures into a           Answers ONLY from that workflow's
  workflow package (steps + assets +        materials, with citations
  FAQs + known errors)                                 │
            │                              ┌───────────┴───────────┐
  Senior glances, fixes, hits Publish      knows it          doesn't know it
            │                                  │                   │
       Workflow is live  ◄───────────────  serves answer      pings owner →
            ▲                               + receipts         senior's reply
            │                                                  becomes new material
            └──────────────── the gap loop feeds back ─────────────────┘
```

The magic is where the two loops touch: **an unanswered question in the Ask loop becomes a pre-filled task in the Feed loop.** The senior doesn't start from a blank page — they get "14 people asked how to roll back a bad deploy, here's a draft I built from the #platform thread, fix and publish?"

---

## 2. Core principle — the Workflow Package

Everything a workflow knows lives in one bundle. This is the atom of Spacebot.

```
WORKFLOW: DEPLOY.PROD_ROLLBACK
├─ Identity      name, ID, category, owner (a real senior), status, version
├─ Steps         ordered, each with: instruction, "how do I know it worked",
│                the screenshot for that step, a video clip, tips, gotchas
├─ Assets        the raw materials seniors fed in — PDFs, PNGs, videos,
│                transcripts — kept whole, with provenance (page / timestamp)
├─ FAQs          drafted from the assets + mined from real questions
├─ Known errors  "ERR_LEASE_HELD → do this" — matched by error code
└─ Related       prerequisites / next steps / alternatives (links to other workflows)
```

When Spacebot answers, it loads *one* (occasionally 2–3 candidate) package and works only inside it. Small, scoped, auditable.

---

## 3. Data model (workflow-centric, single team)

No multi-tenant machinery — one team, one deployment. Postgres + pgvector + full-text is the whole datastore.

```sql
-- People & access (light — it's one team)
users(id, email, name, avatar_url, is_senior bool, teams text[])
-- is_senior gates who can publish/verify. That's most of the authz you need.

-- The catalog
categories(id, parent_id, key, name)          -- Procurement, Deploy, Onboarding…
workflows(
  id, workflow_key text UNIQUE,               -- 'DEPLOY.PROD_ROLLBACK' (human + prompt facing)
  name, summary, category_id,
  owner_user_id,                              -- the senior responsible
  status,                                     -- draft | in_review | published | stale
  current_version int,
  health_score numeric,                       -- completeness, freshness, coverage
  trigger_phrases text[],                     -- ways people ask for this (seed + auto-grown)
  created_at, updated_at, last_verified_at
)

workflow_steps(
  id, workflow_id, order_index,
  title, body_md,
  verification text,                          -- "how do I know this step worked"
  primary_asset_id uuid,                      -- the screenshot/clip for this step
  clip_start_sec int,                         -- deep-link into a video
  tips text[], common_mistakes text[]
)

-- Raw materials, kept whole, owned by ONE workflow (your call: workflow-scoped)
assets(
  id, workflow_id,                            -- ⬅ segregation happens here
  kind,                                       -- pdf | image | video | transcript | link
  storage_key, mime, bytes, checksum,
  original_filename, uploaded_by, uploaded_at,
  status,                                     -- uploaded | processing | ready | needs_review | failed
  extracted jsonb,                            -- OCR text, transcript, detected screen/action
  contains_secret bool,                       -- flagged by the secret/PII scan
  source_page_map jsonb,                      -- text → page number, for citations
  captured_at, source_url                     -- staleness signals for screenshots
)

faqs(id, workflow_id, question, answer_md, source, asked_count)
known_errors(id, workflow_id, code, signature, cause_md, resolution_md, step_key)
workflow_relations(from_id, to_id, kind)      -- prerequisite | next | related | alternative

-- Retrieval index (derived — can be rebuilt from assets/steps any time)
retrieval_units(
  id, workflow_id,                            -- ⬅ EVERY unit carries its workflow_id
  unit_type,                                  -- step | asset_chunk | faq | known_error
  source_id, source_ref,                      -- e.g. {"page": 4} or {"start_sec": 202}
  text, embedding vector(1024),
  tsv tsvector,                               -- full-text, great for error codes / jargon
  updated_at
)
-- Retrieval ALWAYS filters by workflow_id first. That's the whole trick.

-- The learning loop
questions(id, user_id, text, normalized, asked_at, channel)
answers(
  id, question_id, workflow_id, version,
  answer_doc jsonb,                           -- structured answer served
  citations jsonb,                            -- [{step_id | asset_id, page/sec}]
  confidence numeric, abstained bool,
  latency_ms, cost_usd
)
feedback(id, answer_id, user_id, verdict, comment)   -- up | down | wrong_workflow | outdated
gaps(
  id, cluster_key, representative_question, count,
  suggested_workflow_id, draft_ready bool,           -- pre-built draft waiting for a senior
  status, owner_user_id                              -- open | drafted | assigned | resolved
)
```

**The one line that makes RAG safe here:** `retrieval_units` always carries `workflow_id`, and every search is `WHERE workflow_id = ANY($candidates)`. A rollback question can *never* accidentally retrieve a procurement PDF. That containment is worth more than any prompt engineering.

---

## 4. The Feed Layer — the part you actually asked me to nail

This is the hero. Your requirement: **seniors feed it themselves, keyed by Workflow ID, so you never manually collect files and type things in.** Here's the layer that makes that real.

### 4.1 Design rules

1. **A senior's whole job is: pick a Workflow ID, dump materials, glance at the draft, hit Publish.** Everything between "dump" and "draft" is Spacebot's job, not theirs.
2. **Never show a blank form.** The senior uploads raw material; Spacebot produces the structured draft; the senior *edits*, never authors from scratch. Editing takes 5 minutes; authoring takes 2 hours and won't happen.
3. **Meet them where they are.** Upload page, Slack forward, email-in, or record-in-browser. Four doors, one result: material attached to a Workflow ID.

### 4.2 The five ways a senior feeds Spacebot

| Door | What the senior does | What Spacebot does |
|---|---|---|
| **Drop files** | Drags a PDF + 3 PNGs + an MP4 onto a workflow | Structures into steps, attaches each asset to the right step, drafts FAQs/errors |
| **Record now** | Clicks "Record", does the task while narrating (screen + voice) | Transcribes, screenshots each click, builds a step-by-step draft with clips |
| **Paste a transcript / thread** | Pastes a Slack thread or a call transcript | Extracts the procedure and the Q&A into steps + FAQs |
| **Forward to Spacebot** | Forwards an email or Slack message to a Spacebot address/bot, tags the workflow | Same pipeline, zero app-switching |
| **Bulk drop** | Drops a *folder* of 20 docs | Proposes a workflow-split ("these 4 files look like Deploy, these 3 look like Onboarding — confirm?") |

The **Record now** and **Bulk drop** doors are what remove *you* from the loop. A senior records themselves doing a rollback once; a draft workflow appears. A senior dumps their runbook folder; Spacebot proposes the whole catalog. You never chase anyone for files.

### 4.3 The feed screen (what the senior sees)

```
┌─────────────────────────────────────────────────────────────────────┐
│  Feed Spacebot                                          [My drafts 2] │
├─────────────────────────────────────────────────────────────────────┤
│  Which workflow is this for?                                          │
│  ┌───────────────────────────────────────────────────────────────┐   │
│  │ 🔍  DEPLOY.PROD_ROLLBACK                          ✓ exists     │   │
│  │     …or type a new name to create one                          │   │
│  └───────────────────────────────────────────────────────────────┘   │
│                                                                       │
│  Drop your stuff here — I'll figure out the structure                 │
│  ┌───────────────────────────────────────────────────────────────┐   │
│  │   📄 rollback-runbook.pdf     🖼 lease-error.png                │   │
│  │   🎬 rollback-walkthrough.mp4    + paste text   ⏺ Record        │   │
│  └───────────────────────────────────────────────────────────────┘   │
│                                                                       │
│  ⚠ lease-error.png looks like it contains a token — [review & blur]   │
│                                                                       │
│                                              [Build the workflow →]   │
└─────────────────────────────────────────────────────────────────────┘
```

After processing (30–90 sec for a video), the senior lands on a **review card**, not a form:

```
┌─────────────────────────────────────────────────────────────────────┐
│  Draft: DEPLOY.PROD_ROLLBACK          built from 3 sources   [Publish]│
├─────────────────────────────────────────────────────────────────────┤
│  I structured this into 6 steps. Fix anything wrong, then Publish.    │
│                                                                       │
│  ✅ 1  Confirm the bad deploy in Argo         📄 runbook p.2          │
│  ✅ 2  Put the service in maintenance mode    🎬 clip 0:48  📄 p.2    │
│  ⚠ 3  Roll back the release                   🎬 clip 1:30           │
│        ↳ verification: "argo shows previous revision Healthy"         │
│        ↳ ⚠ I wasn't sure about the exact command — please confirm     │
│  ✅ 4  Re-run smoke tests                     🖼 smoke-pass.png       │
│  …                                                                    │
│                                                                       │
│  Known error I found:  ERR_LEASE_HELD → step 3   🖼 lease-error.png   │
│  FAQ I drafted:  "Can I roll back without maintenance mode?"          │
│                                                                       │
│  Owner: @you   ·   Health 78 (missing: screenshot on step 3, step 5)  │
└─────────────────────────────────────────────────────────────────────┘
```

Two details that make this trustworthy and fast:

- **Spacebot flags its own uncertainty** (the ⚠ on step 3). It says *"I wasn't sure about the command"* instead of silently guessing. The senior's edit there is the single most valuable action in the system — it's exactly the knowledge only they have.
- **Health score shows what's missing**, so the senior knows a 5-minute follow-up (add one screenshot) makes the workflow bulletproof. It's a to-do list, not a grade.

### 4.4 Approval = trust boundary

- **Nothing is answerable to new users until a senior clicks Publish.** Drafts are invisible to the Ask loop. This is your protection against garbage-in.
- Published workflows carry a **"verified by @sarah on 2026-07-22"** stamp. New members see who stands behind the answer. That stamp is why they'll trust it.
- Re-verification reminders fire on a schedule (owner picks 90/180 days) so knowledge doesn't rot.

---

## 5. Ingestion pipeline (raw material → workflow package)

Every door in §4.2 funnels into one pipeline. Runs async in a worker; the senior can walk away.

```
Asset lands (attached to workflow_id)
   │
   ├─ checksum dedupe (same file twice = process once)
   ├─ virus scan + type validation (magic bytes, not extension)
   ├─ 🔒 SECRET / PII SCAN  → block publish + offer blur if a token/customer name found
   ├─ strip EXIF/GPS metadata
   │
   ├─ branch by kind ────────────────────────────────────────────────┐
   │   PDF   → text + layout extract, page-mapped; figures OCR'd       │
   │   PNG   → vision model: "what screen is this, what action,        │
   │          what text" + OCR; detect UI state (button disabled etc.) │
   │   VIDEO → transcribe (timestamps + speakers) · scene-detect ·     │
   │          OCR keyframes · segment into topic chunks                │
   │   TEXT  → clean, segment                                          │
   │                                                                   │
   ├─ STRUCTURE (one Opus call): from all this workflow's materials,   │
   │   propose ordered steps, attach the right asset+page/clip to each │
   │   step, draft FAQs, extract known errors, flag uncertainties      │
   │                                                                   │
   ├─ generate alt-text + captions (accessibility + searchability)     │
   ├─ chunk asset text → embed → retrieval_units (tagged workflow_id)  │
   └─ mark ready → notify the senior "your draft is ready"  ───────────┘
```

**Provenance is captured here, not bolted on later.** Every chunk remembers its page number or video second. That's what lets an answer say *"Runbook p.4"* or *"walkthrough 1:30"* — and that citation is what makes the answer trustworthy instead of dangerous.

**Cost/latency note:** OCR and transcription run **once at feed time**, cached by checksum forever. Video is the slow one (a 6-min clip ≈ 60–90 sec to process). Never reprocess at question time.

---

## 6. Retrieval — scoped RAG, the safe kind

Three stages. The first is the one that makes it accurate.

```
New member asks:  "the rollback failed saying lease held, what do I do?"
   │
   ▼
① ROUTE — which workflow(s)?  (Haiku, cheap, ~300ms)
   candidates from, fused together:
     · full-text match on trigger_phrases + workflow names   ("rollback")
     · error-code match: ERR_LEASE_HELD → known_errors → DEPLOY.PROD_ROLLBACK  ⬅ exact hit
     · vector similarity over workflow cards
   → top 1–3 workflows, each with a confidence score
   → if nothing scores high enough → ABSTAIN (see §7)
   │
   ▼
② RETRIEVE — inside those workflows ONLY  (Postgres, ~40ms, no LLM)
   WHERE workflow_id = ANY(candidates):
     · the ordered steps
     · hybrid search (vector + full-text) over that workflow's asset chunks
     · matching known_errors + FAQs
   → a small, tight, on-topic context. Nothing from unrelated workflows can leak in.
   │
   ▼
③ COMPOSE — answer from that context, with citations  (Sonnet)
   → structured answer: the relevant steps, the screenshot, a video clip
     starting at the right second, and "if it's the lease error, do X"
   → every claim tagged with its source (step id / page / timestamp)
```

Why routing-then-scoped-search beats classic "search everything":
- A rollback question physically cannot retrieve a procurement doc — different `workflow_id`, filtered out in SQL.
- Error codes and internal jargon hit via **exact full-text match**, where embeddings are weak. `ERR_LEASE_HELD` matches the known-error row directly.
- The context handed to the model is tiny and on-topic, so the model has little room to wander.

**The router can be wrong too.** So it returns up to 3 candidates and a confidence, and when the top choice is shaky the answer offers a chip: *"I think this is about Prod Rollback — or did you mean Staging Rollback?"* A wrong guess degrades into a question, not a confident lie.

---

## 7. The hallucination defense (you raised this — here's the real answer)

You said it can confidently give false answers. Correct, and no model choice fixes it. Five layers do. They stack.

**Layer 1 — Containment.** Scoped retrieval (§6) means the model only ever sees one workflow's vetted materials. Most hallucinations come from irrelevant retrieved text; segregation removes the fuel.

**Layer 2 — Grounding rule.** The composer is instructed and structurally constrained: *answer only from the provided workflow package; every statement must cite a step or an asset; if the package doesn't contain the answer, say so — do NOT use general knowledge about how deployment/procurement usually works.* That last clause matters: the model *knows* how tools generally behave and will "helpfully" invent SpaceLabs specifics. It's forbidden.

**Layer 3 — Citations, enforced by checking.** The answer is a structured object where each block carries citations. After composing, a deterministic check computes **citation coverage** = fraction of claims backed by a real source. Low coverage → the answer is downgraded or suppressed. This is a *count*, not a vibe.

**Layer 4 — Confidence gate → three behaviors.**

| Confidence | What the new member sees |
|---|---|
| **High** | The answer, with citations and a video clip. Normal. |
| **Medium** | The answer **plus** a banner: *"Best match: Prod Rollback — not what you meant?"* + a "ping @owner" button. |
| **Low / no workflow** | **No answer.** *"I don't have this documented yet. I've asked @sarah (owns Deploy). I'll message you when it's added."* + auto-creates a gap. |

**Layer 5 — Abstention is a feature, and it's how you keep trust.** A Spacebot that says *"I don't know, asking Sarah"* is one people keep using. A Spacebot that always answers is one they stop trusting after the third confident miss — and for a new hire following instructions blindly, a confident miss can mean a broken prod deploy. **Target 5–15% abstention early.** 0% means the gate is broken. Track it as a headline number.

**Every answer shows its work.** Footer on each answer: `DEPLOY.PROD_ROLLBACK · verified by @sarah 3 days ago · from runbook p.4 + walkthrough 1:30`. The new member can click through to the exact source. Receipts are what turn "dangerous" into "trustworthy."

---

## 8. The Ask experience (new member)

```
┌────────────────────────────────────┬──────────────────────────────┐
│  ▸ rollback failed, lease held      │  🔧 Prod Rollback — lease held│
│                                     │  DEPLOY.PROD_ROLLBACK         │
│  That's a known one. The previous   │  ✔ verified by @sarah · 3d ago│
│  rollback still holds the lease.    │  ─────────────────────────────│
│  Do this:                           │  ☐ 1 Check the stuck lease    │
│                                     │       kubectl … [copy]        │
│  1. Check the stuck lease           │  ☐ 2 Force-release it         │
│  2. Force-release it                │       🎬 clip 1:30            │
│  3. Re-run the rollback             │  ☐ 3 Re-run rollback          │
│                                     │       🖼 argo-healthy.png     │
│  [Ask a follow-up…]                 │  ─────────────────────────────│
│                                     │  📄 Source: runbook p.4       │
│                                     │  Still stuck? → @sarah #deploy│
└────────────────────────────────────┴──────────────────────────────┘
```

- Left = the conversation (ephemeral). Right = the **workflow panel** (persistent, checkable, sourced). New members don't scroll up to re-find step 2.
- Available in the **web app** and in **Slack** (ask in the channel you already use — the highest-adoption surface, and it captures knowledge where it's already being transferred).
- Feedback is specific: 👍 / 👎 / "wrong workflow" (which becomes a routing correction) / "this step is outdated" (which pings the owner).

---

## 9. Self-improving loop

The corpus starts thin and gets better *from use*, with no model retraining:

- **Unanswered question → gap.** Similar unanswered questions cluster; when a cluster gets big, Spacebot **pre-drafts** a workflow from whatever raw signal exists (Slack threads, a related doc) and drops it in the owner's queue: *"9 people asked this, here's a 70% draft, review?"* Demand-ranked, so seniors spend their scarce time on what people actually need.
- **Repeated question → FAQ.** Auto-promoted into the relevant workflow.
- **Confirmed answer → trigger phrase.** When "wrong workflow" is corrected, the correct question is added to that workflow's `trigger_phrases`. Routing gets better tomorrow because of a mistake today. Zero ML work.
- **"Outdated" flag → re-verify task** for the owner. This is how you fight decay, which is what kills every internal knowledge base eventually.

---

## 10. Tech stack (deliberately small — it's one team)

| Layer | Choice | Why |
|---|---|---|
| App (ask UI + feed UI) | **Next.js + TypeScript**, Tailwind, shadcn | One app, two surfaces. Streaming answers, rich upload/review UI. |
| API | Next route handlers / a small **Fastify** service | No microservices at this size. |
| Ingestion worker | **Python** (FastAPI + a queue) | PDF/OCR/video/transcription tooling is best in Python. |
| Datastore | **Postgres + pgvector + tsvector** | Vectors, full-text, and relations in one DB. No separate vector DB — corpus is small and scoped, so you don't need one. |
| Object storage | **S3 / Cloudflare R2** | Raw assets + derivatives. R2 = no egress fees for media-heavy answers. |
| Queue/cache | Redis + a Postgres-backed job queue | Enqueue ingestion jobs transactionally. |
| Models | **Claude Haiku** (route) · **Sonnet** (answer) · **Opus** (offline: structure a video/PDF into a draft) | Cheap model for the frequent cheap job; strong model only for composing; best model for the high-leverage offline drafting. |
| Video | Managed transcription (Deepgram/AssemblyAI/Whisper) + a hosted transcode (Mux/Cloudflare Stream) | Don't run a GPU/ffmpeg fleet for one team. |
| Vision/OCR | Claude vision for screenshots; a doc-OCR service for scanned PDFs | Vision reads *what the screenshot means*, not just its text. |

**Provider abstraction from day one.** Wrap all model calls behind one interface. If SpaceLabs later says "data can't leave our cloud," you swap to **Bedrock or Azure** as a config change, not a rewrite. (Given SpaceLabs may be a regulated/medical context, assume this will come up — build the seam now.)

Deploy: a few containers on **ECS Fargate** (or Fly.io to start). Not Kubernetes — wrong tool for one team.

---

## 11. End-to-end walkthrough (one workflow's whole life)

Concrete, start to finish, so the flow is unambiguous.

**Monday — Sarah feeds it (5 min of her time).**
1. Sarah opens Spacebot → Feed. Types "Prod Rollback" → creates `DEPLOY.PROD_ROLLBACK`.
2. She drags in `rollback-runbook.pdf`, a screenshot of the lease error, and a 6-min screen recording where she narrates a real rollback. Clicks **Build the workflow**.
3. Spacebot (worker): transcribes the video, screenshots each click, OCRs the error image (flags a token in it → Sarah blurs it in one click), reads the PDF page-by-page, and **Opus assembles a 6-step draft** — each step linked to a video clip and/or PDF page, plus a drafted FAQ and the `ERR_LEASE_HELD` known-error.
4. Sarah sees the review card. Step 3's command was ambiguous in the video; Spacebot flagged it ⚠. She types the correct command, glances at the rest, clicks **Publish**. It's now stamped *verified by @sarah*.

*You did nothing. You never collected a file or typed a step.*

**Wednesday — Raj (new hire) hits the wall.**
5. A deploy goes bad. Raj types in Slack: *"@spacebot rollback failed, lease held."*
6. Route: `ERR_LEASE_HELD` exact-matches the known-error → `DEPLOY.PROD_ROLLBACK`, high confidence.
7. Retrieve: only that workflow's steps + the lease-error material.
8. Compose: the 3-step fix, the screenshot, a video clip starting at 1:30, citation to runbook p.4.
9. Raj follows it, unblocks himself in 2 minutes. He never pinged Sarah. Sarah never knew it happened. 👍

**Thursday — the gap loop closes.**
10. Someone asks *"how do I roll back just the database, not the service?"* Spacebot has no material for that → **abstains**, tells the asker "I've asked Sarah," and files a gap.
11. Friday, Sarah gets one queue item: *"3 people asked about DB-only rollback. Here's a draft from the #deploy thread. Review?"* Two minutes later there's a new related workflow. The gap is closed **before** it becomes a recurring interruption.

That loop, running continuously, is Spacebot.

---

## 12. Build plan — demo first, then hand the keys to seniors

You said it: get a demo working, *then* seniors self-serve. Sequenced for exactly that.

**Milestone 1 — The demo (you seed it yourself).**
- Postgres schema, workflow + step + asset model, object storage.
- Ingestion for **PDF + PNG** first (video next — it's the slow part).
- The scoped route → retrieve → compose pipeline with citations + abstention.
- A basic ask UI.
- *You* hand-feed 3–5 real workflows so there's something to demo.
- **Exit:** you ask 15 real questions on camera and it answers with receipts and abstains cleanly on the ones it shouldn't answer. This is the demo that convinces the seniors.

**Milestone 2 — Open the Feed Layer to seniors.**
- The feed screen (§4.3), drag-drop + auto-structure, the review card, Publish.
- Add **video** ingestion (transcribe → keyframes → draft) — this is what makes feeding effortless and is worth the wait.
- Secret/PII scan on upload (do not skip — a leaked token in a screenshot is a real incident).
- **Exit:** three seniors each feed a couple of workflows *without you touching anything*. If they can't, the auto-structuring isn't good enough yet — that's the thing to fix, not the UI.

**Milestone 3 — The loop + where people already are.**
- Slack bot (ask + "@spacebot").
- Feedback capture, gaps, pre-drafted gap tasks, FAQ promotion, health scores.
- **Exit:** an unanswered question produces a ranked, pre-drafted task a senior finishes in 2 minutes.

**Not now:** proactive/browser-watching features, executing actions, anything multi-tenant. The multi-tenant SaaS you mentioned is a *wrapper over this exact core* — prove the core with SpaceLabs first, then the wrapper is mostly auth + per-team isolation.

---

## 13. Assumptions & honest risks

**Assumptions I made (correct me and I'll adjust):**
- Hosted Claude API for now, behind a provider seam so you can move to Bedrock/Azure if SpaceLabs requires data stay in-house.
- Slack is the team's chat (swap for Teams if not).
- Content is mostly loose files + recordings the seniors have, not a giant existing wiki.

**The real risks, ranked:**
1. **Seniors don't feed it.** #1 killer. Mitigation is the entire Feed Layer — the win is that they contribute *raw material they already have* (a screen recording, a PDF) and Spacebot does the structuring. If they still won't, the auto-structuring or the friction is the problem; fix that before anything else.
2. **A confident wrong answer misleads a new hire.** Mitigated by §7's five layers — containment, grounding, citation-counting, confidence gating, and abstention. The honest posture: Spacebot answers less often but is trusted more. That trade is correct for a guidance tool.
3. **Knowledge rots.** Screenshots go stale, procedures change. Mitigated by re-verify reminders, staleness signals on assets, and the "this is outdated" button. It's a permanent gardening job, not a one-time fix — the health score keeps it visible.
4. **Cold start.** Empty on day one. Mitigated by you seeding the demo workflows, then the gap loop growing the corpus from real questions.

---

### The one thing to hold onto

Spacebot is not a chatbot with your docs bolted on. It's a machine that turns **one senior explaining something once** into **a permanent, sourced, self-correcting answer** — organized by workflow so it stays accurate. Build the feed layer so good that seniors *want* to dump their knowledge into it, keep it honest with citations and abstention, and you've built something a generic RAG bot can't touch. The SaaS is the easy part after that.
