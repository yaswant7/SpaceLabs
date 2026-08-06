"""Ingestion orchestration — the async job that runs:

  store blobs -> decompose (adapters) -> enrich (vision/secret scan) -> structure (LLM
  over Segments) -> workflow draft (status 'in_review', awaiting a senior's Publish).

Runs in a background thread here; in production this is a durable queue/worker (Temporal/
Celery) so long video jobs survive restarts. The stage transitions are identical either way,
which is why the POC models it as a job with status rather than an inline call.
"""
import hashlib
import re
import threading

from .. import db, providers
from . import adapters
from .blob import store
from .capabilities import CapabilityUnavailable, get_image_understander, scan_secrets


# Below this much extracted text (and with no images), we refuse to structure rather
# than let the model fill the gap. Roughly a short paragraph.
MIN_MATERIAL_CHARS = 200


def start_ingest(wf_key, name, category, owner, files, actor="") -> str:
    """files: [{filename, mime, bytes}]. Returns a job id to poll.

    `actor` is who is doing this, recorded on the workflow so the audit trail can answer
    "who brought this in" — distinct from `owner`, which is who is responsible for the
    content afterwards and may be somebody else entirely.
    """
    job_id = db.create_job(None, wf_key, len(files))
    t = threading.Thread(target=_run,
                         args=(job_id, wf_key, name, category, owner, files, actor),
                         daemon=True)
    t.start()
    return job_id


def _run(job_id, wf_key, name, category, owner, files, actor=""):
    try:
        db.update_job(job_id, status="running", stage="storing files")
        blob = store()
        descs, notes, order = [], [], 0
        source_names = {}

        for idx, f in enumerate(files):
            data = f["bytes"]
            kind = adapters.guess_kind(f.get("mime", ""), f["filename"])
            blob_key = blob.put(data, f["filename"].split(".")[-1] if "." in f["filename"] else "")
            checksum = hashlib.sha256(data).hexdigest()
            source_id = db.create_source(job_id, None, kind, f["filename"], f.get("mime", ""),
                                         blob_key, checksum, len(data), idx)
            source_names[source_id] = f["filename"]
            db.update_job(job_id, stage=f"decomposing {f['filename']} ({kind})")
            adapter = adapters.pick_adapter(f.get("mime", ""), f["filename"])
            try:
                for d in adapter.decompose(data, f["filename"]):
                    d["_source_id"] = source_id
                    d["_order"] = order
                    order += 1
                    descs.append(d)
                db.set_source_status(source_id, "decomposed")
            except CapabilityUnavailable as e:
                db.set_source_status(source_id, "awaiting_capability", {"reason": str(e)})
                notes.append(f"{f['filename']}: {e}")

        # enrich + persist segments
        db.update_job(job_id, stage="understanding images & scanning for secrets")
        iu = get_image_understander()
        secrets_found = 0
        for d in descs:
            img_key = None
            meta = d.get("meta", {}) or {}
            if d.get("image_bytes"):
                img_key = blob.put(d["image_bytes"], "png")
                try:
                    desc = iu.describe(d["image_bytes"])
                    d["text"] = d.get("text") or desc.get("_text", "")
                    meta.update({"contains_secret": desc.get("contains_secret", False),
                                 "alt": desc.get("alt_text", "")})
                except Exception as e:
                    meta["enrich_error"] = str(e)
            elif d.get("text") and scan_secrets(d["text"]):
                meta["contains_secret"] = True
            if meta.get("contains_secret"):
                secrets_found += 1
            d["_image_key"] = img_key
            d["meta"] = meta
            db.add_segment(d["_source_id"], job_id, None, d["modality"], d["_order"],
                           d.get("text", ""), img_key, d.get("anchor"), meta)

        if not descs:
            db.update_job(job_id, status="failed",
                          stage="nothing could be read from those files",
                          error="; ".join(notes) or "no readable content",
                          result={"notes": notes})
            return

        # Refuse to structure near-empty material. The model will happily invent a
        # complete, plausible workflow from a single short line — so the guard has to be
        # here, before it is asked, not in a prompt telling it not to.
        usable = sum(len((d.get("text") or "").strip()) for d in descs)
        images = sum(1 for d in descs if d.get("image_bytes"))
        if usable < MIN_MATERIAL_CHARS and not images:
            db.update_job(
                job_id, status="failed", stage="not enough readable content",
                error=f"only {usable} characters could be extracted — too little to build a "
                      f"workflow from without guessing",
                result={"notes": notes, "extracted_chars": usable})
            return

        # structure Segments -> workflow draft
        db.update_job(job_id, status="structuring", stage="structuring the workflow")
        draft = _structure(descs, name)

        wf = {
            "wf_key": wf_key,
            "name": draft.get("name") or name or wf_key,
            "summary": draft.get("summary", ""),
            "category": category or "Uncategorized",
            "owner": owner or "",
            "status": "in_review",          # NOT answerable until a senior publishes
            "trigger_phrases": draft.get("trigger_phrases", []),
            "subjects": draft.get("subjects", []),
            "actor": actor,
            "steps": draft.get("steps", []),
            "known_errors": draft.get("known_errors", []),
            "faqs": draft.get("faqs", []),
            "verified_by": "",
        }
        wid = db.upsert_workflow(wf)

        # Keep the extracted source text attached to the workflow, per file. Without this
        # the only trace of an uploaded document is the model's structured summary of it,
        # so anything the structurer didn't think to turn into a step or an FAQ becomes
        # unanswerable and unsearchable — which is not what "I uploaded this document"
        # should mean. Retrieval indexes it and the composer can quote from it.
        by_source = {}
        for d in descs:
            if (d.get("text") or "").strip():
                by_source.setdefault(d["_source_id"], []).append(d["text"].strip())
        for src_id, chunks in by_source.items():
            db.add_asset(wid, "text", source_names.get(src_id, "source"),
                         "\n\n".join(chunks),
                         {"origin": "extracted", "segments": len(chunks)})

        # attach the ordered visuals to steps (i-th image segment -> step i)
        image_segs = [d for d in descs if d.get("_image_key")]
        attached = 0
        for i, _step in enumerate(wf["steps"]):
            if i < len(image_segs):
                d = image_segs[i]
                aid = db.add_asset(wid, "image", d.get("meta", {}).get("filename", "screenshot"),
                                   d.get("text", ""), {"blob_key": d["_image_key"], "anchor": d.get("anchor")})
                db.link_step_asset(wid, i + 1, aid)
                attached += 1

        db.update_job(job_id, status="drafted", stage="draft ready for review", result={
            "wf_key": wf_key, "name": wf["name"], "status": "in_review",
            "step_count": len(wf["steps"]), "segment_count": len(descs),
            "images_attached": attached, "secrets_flagged": secrets_found,
            "uncertain": draft.get("uncertain", []), "notes": notes,
            "provider": providers.get_provider().name,
        })
    except Exception as e:
        db.update_job(job_id, status="failed", stage="error", error=repr(e))


_TAG = re.compile(r"^\s*\[(?:text|image|pdf_page|video_scene|audio)\b[^\]]*\]\s*")


def _clean_draft(draft):
    """Strip the "[text para=1]" provenance tags out of anything the model wrote.

    The tags are prepended to each segment so the structurer knows where material came
    from, but models copy them straight into the summary and step bodies — and the
    summary is what the router reads, so the noise costs retrieval quality as well as
    looking sloppy in the UI.
    """
    def clean(v):
        return _TAG.sub("", v).strip() if isinstance(v, str) else v

    for key in ("name", "summary"):
        draft[key] = clean(draft.get(key, ""))
    for s in draft.get("steps") or []:
        for key in ("title", "body", "verification"):
            s[key] = clean(s.get(key, ""))
    for f in draft.get("faqs") or []:
        for key in ("question", "answer"):
            f[key] = clean(f.get(key, ""))
    draft["trigger_phrases"] = [clean(t) for t in (draft.get("trigger_phrases") or []) if clean(t)]
    # Subjects are trusted downstream to decide which document a question is about, so a
    # malformed one is worse than a missing one: keep only short, plain strings.
    draft["subjects"] = [clean(str(s)) for s in (draft.get("subjects") or [])
                         if clean(str(s)) and len(clean(str(s))) <= 80][:8]
    return draft


def _structure(descs, name):
    """Serialise all Segments (with modality + anchor tags) into one material blob and
    let the structuring model build the workflow. The model sees uniform Segments — it
    never knows some came from a PDF and some from screenshots."""
    lines = []
    for d in descs:
        a = d.get("anchor", {})
        tag = d["modality"] + ("" if not a else " " + ",".join(f"{k}={v}" for k, v in a.items()))
        lines.append(f"[{tag}] {d.get('text','').strip()}".strip())
    material = "\n\n".join(l for l in lines if l.strip())
    draft = _clean_draft(providers.get_provider().structure(material))
    if name:
        draft["name"] = name
    return draft
