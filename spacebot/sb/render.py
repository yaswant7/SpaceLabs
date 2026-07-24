"""Render a structured AnswerDocument to markdown (used by the non-streaming fallback path,
and shared so providers and the server agree on the format)."""


def answer_to_markdown(a: dict) -> str:
    if a.get("abstained"):
        return a.get("headline", "I don't have that documented yet.")
    parts = [a.get("headline", "")]
    for b in a.get("blocks", []):
        t = b.get("type")
        if t == "known_error":
            parts.append(f"**🔧 {b.get('code','')}** — {b.get('resolution','')}")
        elif t == "steps":
            for i, s in enumerate(b.get("steps", []), 1):
                title = (s.get("title") or "").rstrip(".")
                parts.append(f"**{i}. {title}.** {s.get('body','')}".strip())
                if s.get("verification"):
                    parts.append(f"✓ {s['verification']}")
        elif t == "text":
            parts.append(b.get("md", ""))
    return "\n\n".join(p for p in parts if p)
