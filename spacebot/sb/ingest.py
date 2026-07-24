"""Ingestion — raw material a senior provides becomes a structured workflow.

Supported now: .md/.txt (read), .pdf (pypdf), images (vision via the configured provider).
The structuring step (raw text -> steps/faqs/known-errors draft) is the same for every
provider; a real API key produces good drafts, mock mode splits by paragraph.
"""
import os

from . import db
from .providers import get_provider

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gif": "image/gif", ".webp": "image/webp"}


def extract_text(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in (".md", ".txt", ""):
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    if ext == ".pdf":
        try:
            import pypdf
        except ImportError:
            return "[PDF support needs: pip install --user pypdf]"
        reader = pypdf.PdfReader(path)
        out = []
        for i, page in enumerate(reader.pages):
            out.append(f"[page {i + 1}]\n{page.extract_text() or ''}")
        return "\n\n".join(out)
    return ""


def ingest_file(workflow_id: str, path: str) -> dict:
    """Attach one file to a workflow as an asset (with extracted/described text)."""
    ext = os.path.splitext(path)[1].lower()
    fname = os.path.basename(path)
    if ext in IMAGE_EXT:
        prov = get_provider()
        with open(path, "rb") as fh:
            desc = prov.describe_image(fh.read(), MIME.get(ext, "image/png"))
        text = f"{desc.get('screen','')} — {desc.get('action','')}\n{desc.get('text','')}"
        aid = db.add_asset(workflow_id, "image", fname, text.strip(),
                           {"alt_text": desc.get("alt_text", ""),
                            "contains_secret": desc.get("contains_secret", False)})
        return {"asset_id": aid, "kind": "image", "contains_secret": desc.get("contains_secret", False)}
    text = extract_text(path)
    kind = "pdf" if ext == ".pdf" else "text"
    aid = db.add_asset(workflow_id, kind, fname, text)
    return {"asset_id": aid, "kind": kind, "chars": len(text)}


def structure_from_text(wf_key: str, name: str, category: str, owner: str, material: str) -> dict:
    """Turn raw material into a workflow draft and store it. Returns the draft + uncertainties."""
    prov = get_provider()
    draft = prov.structure(material)
    wf = {
        "wf_key": wf_key,
        "name": name or draft.get("name", wf_key),
        "summary": draft.get("summary", ""),
        "category": category or "Uncategorized",
        "owner": owner or "",
        "status": "published",          # POC auto-publishes; real app gates on senior approval
        "trigger_phrases": draft.get("trigger_phrases", []),
        "steps": draft.get("steps", []),
        "known_errors": draft.get("known_errors", []),
        "faqs": draft.get("faqs", []),
        "verified_by": owner or "",
    }
    wid = db.upsert_workflow(wf)
    db.add_asset(wid, "text", "pasted-material.txt", material)
    return {"workflow_id": wid, "wf_key": wf_key, "uncertain": draft.get("uncertain", []),
            "step_count": len(wf["steps"]), "provider": prov.name}
