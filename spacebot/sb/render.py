"""Rendering helpers shared by the providers and the server, so everyone agrees on format.

Two directions:
  * packages_for_prompt — workflow packages → the text the model reads
  * answer_to_markdown  — a structured AnswerDocument → markdown for the reader

The prompt-side renderer exists because raw `json.dumps(package)` is a bad prompt. It
buries the three fields that matter (title, body, verification) inside ids, timestamps and
empty lists, and small local models reliably lose the verification text in the noise. A
flat labelled outline costs fewer tokens and survives a 3B model's attention.
"""


def _clean(v):
    return (v or "").strip()


def package_for_prompt(p: dict) -> str:
    """One workflow package as a labelled outline. Cite ids are included inline so a model
    asked for citations can copy them rather than construct them."""
    key = p.get("wf_key", "")
    out = [f"WORKFLOW: {_clean(p.get('name'))}   [{key}]"]

    meta = []
    if p.get("category"):
        meta.append(_clean(p["category"]))
    if p.get("owner"):
        meta.append(f"owned by @{_clean(p['owner'])}")
    if p.get("verified_by"):
        v = f"verified by @{_clean(p['verified_by'])}"
        if p.get("verified_at"):
            v += f" on {_clean(p['verified_at'])}"
        meta.append(v)
    if meta:
        out.append(" · ".join(meta))
    if p.get("summary"):
        out.append(f"Summary: {_clean(p['summary'])}")

    if p.get("known_errors"):
        out.append("\nKNOWN ERRORS:")
        for e in p["known_errors"]:
            out.append(f"- {_clean(e.get('code'))}  [cite {key}:error]")
            if e.get("cause"):
                out.append(f"    cause: {_clean(e['cause'])}")
            out.append(f"    fix: {_clean(e.get('resolution'))}")

    if p.get("steps"):
        # Verifications and pitfalls are emitted already wearing their output markers.
        # A small model copies what it sees far more reliably than it follows a rule about
        # what to write, so the source format here IS the target format.
        out.append("\nSTEPS (in order):")
        for s in p["steps"]:
            out.append(f"{s.get('order')}. {_clean(s.get('title'))}  [cite {key}:{s.get('key')}]")
            if s.get("body"):
                out.append(f"    {_clean(s['body'])}")
            if s.get("verification"):
                out.append(f"    ✓ {_clean(s['verification'])}")
            for m in s.get("mistakes") or []:
                out.append(f"    Heads up: {_clean(m)}")
            for t in s.get("tips") or []:
                out.append(f"    Tip: {_clean(t)}")
            asset = s.get("asset")
            if asset and asset.get("text"):
                out.append(f"    (screenshot shows: {_clean(asset['text'])[:400]})")

    if p.get("faqs"):
        out.append("\nFAQ:")
        for f in p["faqs"]:
            out.append(f"- Q: {_clean(f.get('question'))}")
            out.append(f"  A: {_clean(f.get('answer'))}")

    # The extracted source document. Included last and generously for reference entries
    # (no steps), where it IS the content — a CV's facts live in the document, not in
    # whatever FAQs the structurer happened to generate.
    extracted = [a for a in (p.get("extra_assets") or [])
                 if a.get("kind") == "text" and (a.get("text") or "").strip()]
    if extracted:
        budget = 6000 if not p.get("steps") else 1500
        out.append("\nSOURCE DOCUMENT (verbatim — quote facts from here):")
        for a in extracted:
            body = _clean(a.get("text"))[:budget]
            out.append(f"--- {_clean(a.get('filename')) or 'document'} ---")
            out.append(body)

    if p.get("related"):
        out.append("\nRELATED WORKFLOWS:")
        for r in p["related"]:
            out.append(f"- {_clean(r.get('name'))} [{_clean(r.get('wf_key'))}] — {_clean(r.get('kind'))}")

    return "\n".join(out)


def packages_for_prompt(packages, limit: int = 14000) -> str:
    text = "\n\n" + ("\n\n" + "=" * 60 + "\n\n").join(
        package_for_prompt(p) for p in (packages or []))
    return text[:limit]


def answer_to_markdown(a: dict) -> str:
    """Structured AnswerDocument → markdown. Used by the non-streaming fallback path."""
    if a.get("abstained") or a.get("abstain"):
        return a.get("headline") or "I don't have that documented yet."
    parts = [a.get("headline", "")]
    n = 0
    for b in a.get("blocks", []):
        t = b.get("type")
        if t == "known_error":
            parts.append(f"**{b.get('code', '')}** — {b.get('resolution', '')}")
        elif t == "steps":
            for s in b.get("steps", []):
                n += 1
                title = (s.get("title") or "").rstrip(".")
                parts.append(f"{n}. **{title}.** {s.get('body', '')}".strip())
                if s.get("verification"):
                    parts.append(f"   ✓ {s['verification']}")
        elif t == "text":
            parts.append(b.get("md", ""))
    return "\n\n".join(p for p in parts if p)
