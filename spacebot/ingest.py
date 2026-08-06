#!/usr/bin/env python3
"""Point this at a folder and it becomes answerable knowledge.

The missing half of "clone it and use your own documents". Everything the web upload does,
without a browser: same adapters, same structuring, same guards — so a file that would be
refused in the UI is refused here too, and for the same stated reason.

    python3 ingest.py ./handbook
    python3 ingest.py ./handbook --publish            answerable immediately
    python3 ingest.py ./runbooks --owner ops --category Operations
    python3 ingest.py ./handbook --dry-run            list what would be ingested

One workflow per file, keyed from the filename. Files already ingested (same key) are
skipped unless --force, so re-running over a growing folder only does the new work.

Nothing leaves the machine. Parsing is local, embeddings and generation go to whichever
provider is configured — point that at Ollama and the documents never leave the building.
"""
import argparse
import mimetypes
import os
import re
import sys
import time

from sb import chunks as chunkstore
from sb import db, retrieval
from sb.media import pipeline as mpipe

SUPPORTED = {".pdf", ".docx", ".docm", ".xlsx", ".xlsm", ".csv", ".tsv", ".html", ".htm",
             ".txt", ".md", ".vtt", ".srt", ".png", ".jpg", ".jpeg", ".webp", ".gif",
             ".bmp", ".tiff", ".mp4", ".mov", ".webm", ".mkv", ".avi",
             ".mp3", ".wav", ".m4a", ".ogg", ".flac"}


def key_for(path: str, prefix: str) -> str:
    stem = os.path.splitext(os.path.basename(path))[0]
    slug = re.sub(r"[^A-Za-z0-9]+", "_", stem).strip("_").upper()[:48] or "DOC"
    return f"{prefix}.{slug}"


def find(root: str) -> list:
    if os.path.isfile(root):
        return [root]
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in sorted(filenames):
            if fn.startswith("."):
                continue
            if os.path.splitext(fn)[1].lower() in SUPPORTED:
                out.append(os.path.join(dirpath, fn))
    return sorted(out)


def main():
    p = argparse.ArgumentParser(description="Ingest a file or folder into the knowledge base.")
    p.add_argument("path", help="file or folder to ingest")
    p.add_argument("--owner", default="admin", help="who owns the resulting entries")
    p.add_argument("--category", default="", help="category for the resulting entries")
    p.add_argument("--prefix", default="DOC", help="workflow key prefix")
    p.add_argument("--publish", action="store_true",
                   help="publish immediately instead of leaving entries in review")
    p.add_argument("--force", action="store_true", help="re-ingest files already present")
    p.add_argument("--dry-run", action="store_true", help="list what would be ingested")
    p.add_argument("--timeout", type=int, default=900,
                   help="seconds to wait per file (local models on CPU are slow)")
    args = p.parse_args()

    if not os.path.exists(args.path):
        print(f"no such path: {args.path}")
        return 1

    db.init_db()
    files = find(args.path)
    if not files:
        print(f"nothing ingestable under {args.path}")
        print(f"supported: {' '.join(sorted(SUPPORTED))}")
        return 1

    existing = {w["wf_key"] for w in db.list_workflows()}
    todo = [f for f in files
            if args.force or key_for(f, args.prefix) not in existing]
    skipped = len(files) - len(todo)

    print(f"{len(files)} file(s) found, {len(todo)} to ingest"
          f"{f', {skipped} already present' if skipped else ''}\n")
    if args.dry_run:
        for f in todo:
            print(f"  would ingest  {key_for(f, args.prefix):32s} {f}")
        return 0

    ok = failed = 0
    for i, path in enumerate(todo, 1):
        wf_key = key_for(path, args.prefix)
        name = os.path.splitext(os.path.basename(path))[0].replace("_", " ").strip()
        with open(path, "rb") as fh:
            data = fh.read()
        mime = mimetypes.guess_type(path)[0] or "application/octet-stream"

        print(f"  [{i}/{len(todo)}] {os.path.basename(path)} … ", end="", flush=True)
        job = mpipe.start_ingest(wf_key, name, args.category, args.owner,
                                 [{"filename": os.path.basename(path),
                                   "mime": mime, "bytes": data}])
        started = time.time()
        status = "?"
        while time.time() - started < args.timeout:
            status = (db.get_job(job) or {})["status"]
            if status in ("drafted", "failed"):
                break
            time.sleep(1)

        j = db.get_job(job) or {}
        if status == "drafted":
            ok += 1
            print(f"ok ({time.time() - started:.0f}s)")
            if args.publish:
                db.set_workflow_status(wf_key, "published")
        elif status == "failed":
            failed += 1
            # Surfaced verbatim: refusing a file is a designed outcome, and the reason is
            # the actionable part — a scanned PDF with no text layer needs OCR, not a retry.
            print(f"refused — {(j.get('error') or 'no reason recorded')[:110]}")
        else:
            failed += 1
            print(f"still running after {args.timeout}s — raise --timeout")

    if ok:
        # Chunk and embed now, so the first question after ingesting is fast rather than
        # paying for the whole corpus.
        print("\nindexing …")
        chunkstore.reindex_all()
        while True:
            r = chunkstore.embed_pending()
            if not r.get("embedded"):
                break
            print(f"  embedded {r['embedded']}, {r.get('pending', 0)} left")
        retrieval.invalidate()
        print(f"  {chunkstore.stats()}")

    print(f"\n{ok} ingested, {failed} refused"
          f"{'' if args.publish else ' — entries are in review until published'}")
    return 1 if failed and not ok else 0


if __name__ == "__main__":
    sys.exit(main())
