# Spacebot — Multimodal Ingestion

The author's material will never be uniform: a PDF (often text **plus** embedded
screenshots), a single screenshot, an ordered **sequence** of screenshots, a screen-
recording video, an audio note, or a pasted transcript. Spacebot handles all of them with
**one pipeline**, because the structuring engine never sees the file type — it only ever
sees **Segments**.

```
raw upload ──► ADAPTER (per modality) ──► Segments ──► ENRICH ──► STRUCTURE ──► workflow draft ──► (senior Publish)
                    ▲                        │            ▲              │
             one adapter per            uniform IR   capability     LLM over Segments
             modality; add a            (text +      providers      (modality-agnostic)
             modality = add an          image_ref +  (transcribe,
             adapter, nothing           anchor)      frames, OCR)
             downstream changes                      swap local↔cloud↔in-house
```

## The Segment — the one representation everything becomes

```
Segment {
  modality       text | image | pdf_page | video_scene | audio
  order_index    global order across the whole upload batch (a PNG sequence stays ordered)
  text           extracted / OCR'd / transcribed / vision-described text
  image_blob_key the representative visual, if any (content-addressed)
  anchor         {page:4} | {image_index:2} | {t_start:134,t_end:161}   ← provenance for citations
  meta           {contains_secret, speaker, ui_elements, ...}
}
```

A PDF-with-images, a 6-screenshot sequence, and a narrated video all collapse into the same
ordered list of Segments. That is why adding a modality is additive, and why every answer can
cite back to *page 4 / screenshot 2 / video 2:14*.

## Two seams (both already in the code)

**1. Adapters** — `sb/media/adapters.py`. One class per modality turns a raw upload into
Segment descriptors. Registered by mime/extension.

| Modality | Adapter | Status in this repo |
|---|---|---|
| PDF (text + embedded images) | `PdfAdapter` | ✅ works (pypdf: text per page + each embedded image → its own segment) |
| Screenshot / PNG sequence | `ImageAdapter` | ✅ works (order preserved across the batch) |
| Transcript / notes / markdown | `TranscriptAdapter` | ✅ works |
| Video (screen recording) | `VideoAdapter` | ⚙ wired to capabilities; needs a frame-extractor + transcriber |
| Audio | `AudioAdapter` | ⚙ wired; needs a transcriber |

*Add a modality:* write one `decompose()` and register it. Nothing else changes.

**2. Capability providers** — `sb/media/capabilities.py`. The heavy, external, swappable
operations, behind interfaces so local↔cloud↔the customer's own service is a config change.

| Capability | POC now | Production swap (implement the same interface) |
|---|---|---|
| Image understanding | LLM vision (`describe_image`) + secret scan | same, or add layout OCR (Textract/DocAI) |
| Transcription | `LocalWhisper` (if binary present) | `CloudTranscriber` → Deepgram / AssemblyAI / **customer endpoint** |
| Frame extraction | `LocalFFmpeg` (if present) | `MuxProvider` / Cloudflare Stream |
| Embeddings (future scaled retrieval) | not needed at this corpus size | local `bge` or hosted, behind an interface |

If a capability isn't configured, the job records **"awaiting capability"** and keeps the
original blob — it never fails or guesses. When you plug the provider in and re-run, the video
becomes scenes+transcript with no other code change.

## Async jobs, storage, provenance

- **Jobs** (`sb/media/pipeline.py`): `store → decompose → enrich → structure → draft`, each stage
  status-tracked and polled (`/api/jobs/{id}`). In the POC it's a background thread; in production
  the identical stages run on a **durable queue/worker** (Temporal/Celery) so a 40-minute video
  survives a restart. The stage transitions don't change.
- **Blob store** (`sb/media/blob.py`): content-addressed (sha256) so identical uploads dedupe.
  `LocalDiskBlob` now; `S3Blob`/`R2Blob`/customer bucket implement the same interface. Production
  adds **presigned direct-to-bucket uploads** so large media never proxies through the API.
- **Approval gate**: ingested workflows are `in_review` and **not answerable** until a senior
  clicks Publish. Garbage-in can't reach end users.
- **Provenance**: every Segment keeps its `anchor`, so a published step cites its source
  (pdf page / screenshot index / video timestamp) — the anti-hallucination receipt.

## Data residency / "beyond my PC"

Nothing here is bound to the laptop. Because capabilities and storage are interfaces:
- Keep everything **in the customer's cloud**: point `BlobStore` at their bucket, `Transcriber`
  at their in-house ASR (or Bedrock/Azure), and the LLM provider at their Bedrock/Azure endpoint.
- The pipeline code is identical; only the provider implementations and config differ.

## To make video real (the one remaining wiring)

1. Implement `CloudTranscriber.transcribe()` (Deepgram/AssemblyAI) → return `segments` with
   `{start, end, text, speaker}` and word timestamps.
2. Implement `LocalFFmpeg.scenes()` (or `MuxProvider`) → return `[{t_start, t_end, keyframe_bytes}]`.
3. Set `transcriber` / `transcriber_api_key` in settings. `VideoAdapter` already aligns them into
   `video_scene` Segments and attaches keyframes to steps — no pipeline change needed.
