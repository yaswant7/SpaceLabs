"""Ingestion orchestration — the async job that runs:

  store blobs -> decompose (adapters) -> enrich (vision/secret scan) -> structure (LLM
  over Segments) -> workflow draft (status 'in_review', awaiting a senior's Publish).

Runs in a background thread here; in production this is a durable queue/worker (Temporal/
Celery) so long video jobs survive restarts. The stage transitions are identical either way,
which is why the POC models it as a job with status rather than an inline call.
"""
import hashlib
import threading

from .. import db, providers
from . import adapters
from .blob import store
from .capabilities import CapabilityUnavailable, get_image_understander, scan_secrets


def start_ingest(wf_key, name, category, owner, files) -> str:
    """files: [{filename, mime, bytes}]. Returns a job id to poll."""
    job_id = db.create_job(None, wf_key, len(files))
    t = threading.Thread(target=_run, args=(job_id, wf_key, name, category, owner, files), daemon=True)
    t.start()
    return job_id


def _run(job_id, wf_key, name, category, owner, files):
    try:
        db.update_job(job_id, status="running", stage="storing files")
        blob = store()
        descs, notes, order = [], [], 0

        for idx, f in enumerate(files):
            data = f["bytes"]
            kind = adapters.guess_kind(f.get("mime", ""), f["filename"])
            blob_key = blob.put(data, f["filename"].split(".")[-1] if "." in f["filename"] else "")
            checksum = hashlib.sha256(data).hexdigest()
            source_id = db.create_source(job_id, None, kind, f["filename"], f.get("mime", ""),
                                         blob_key, checksum, len(data), idx)
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
            db.update_job(job_id, status="failed", stage="nothing to structure",
                          result={"notes": notes})
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
            "steps": draft.get("steps", []),
            "known_errors": draft.get("known_errors", []),
            "faqs": draft.get("faqs", []),
            "verified_by": "",
        }
        wid = db.upsert_workflow(wf)

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
    draft = providers.get_provider().structure(material)
    if name:
        draft["name"] = name
    return draft
