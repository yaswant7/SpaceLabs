#!/usr/bin/env python3
"""The stream cleaner must work on a STREAM, not just on whole strings.

Tokens arrive a few characters at a time, so "the excerpts" reaches the filter as "the",
" excerpt", "s". A per-token replace sees none of those phrases and silently does nothing —
which is exactly how a banned phrase reached a user while the substitution "worked" when
tested on a complete sentence.

No model needed; this is pure text handling.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sb import postprocess as pp   # noqa: E402

fails = []


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'} {label}{'  ' + detail if detail else ''}")
    if not cond:
        fails.append(label)


def as_tokens(text, size=3):
    """Chop into small pieces the way a model emits them."""
    return [text[i:i + size] for i in range(0, len(text), size)]


def run(text, size=3):
    return "".join(pp.clean_stream(iter(as_tokens(text, size))))


print("== internal vocabulary never reaches the reader ==")
CASES = [
    "The excerpts do not mention his programming languages.",
    "Based on the context, we have nothing on that.",
    "The knowledge base has no record of this.",
    "These excerpts are about somebody else.",
    "I checked the provided documents and found nothing.",
]
for text in CASES:
    for size in (1, 3, 7, 200):          # 200 = arrives whole
        out = run(text, size)
        clean = not any(w in out.lower() for w in
                        ("excerpt", "the context", "knowledge base", "provided document"))
        if not clean:
            check(f"{text[:34]!r} at {size}-char tokens", False, out)
            break
    else:
        check(f"{text[:34]!r} cleaned at every token size", True)

print("\n== nothing else is disturbed ==")
keep = "Roll back the deploy in Argo CD, then confirm the pods are healthy."
check("ordinary text passes through unchanged", run(keep) == keep, run(keep))

multi = "First line here.\nSecond line follows on."
check("newlines survive", run(multi) == multi, repr(run(multi)))

long = "word " * 200
check("long output is not truncated", run(long).strip() == long.strip(),
      f"{len(run(long))} vs {len(long)} chars")

print("\n== the bolted-on title is still dropped ==")
titled = "**Roll back a production deploy**\nOpen Argo CD and pick the previous revision."
out = run(titled)
check("title line removed", not out.strip().startswith("**Roll back"), out[:50])
check("body kept", "Open Argo CD" in out, out[:50])

print("\n== a single short answer still emerges ==")
short = "We don't have that."
check("short text passes", run(short) == short, repr(run(short)))

print(f"\n{'PASS' if not fails else 'FAIL'} — {len(fails)} failing")
for f in fails:
    print(f"  - {f}")
sys.exit(1 if fails else 0)
