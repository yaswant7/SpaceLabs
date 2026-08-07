"""Stream transforms applied between the model and the user.

Everything here exists because prompting alone doesn't hold reliably on a 3B model, and
each of these is cheap, deterministic and safe to apply to a token stream. They are
transforms, never rewrites: nothing invents or reorders content.
"""
import re

from . import db

_TITLE_LINE = re.compile(r"^\s*(#{1,6}\s+.*|\*\*[^*\n]{1,90}\*\*:?)\s*$")

# Workflow IDs look like DEPLOY.PROD_ROLLBACK or PEOPLE.YASWANTH_CV. They are internal
# addressing, and a colleague would never say one out loud.
_WF_ID = re.compile(r"\b[A-Z][A-Z0-9]{1,}(?:[._][A-Z0-9_]{2,})+\b")

_names = {"at": 0, "map": {}}


def _key_to_name():
    """wf_key -> human name, refreshed lazily. Small and cached; catalogue reads are cheap
    but this runs per token buffer."""
    cards = db.get_catalog()
    if len(cards) != _names["at"]:
        _names.update({"at": len(cards), "map": {c["wf_key"]: c["name"] for c in cards}})
    return _names["map"]


def humanise_ids(text: str) -> str:
    """Replace internal workflow IDs with their human names.

    The model is told not to emit them and mostly complies, but "follow the
    PROC.VENDOR_APPROVAL procedure" slips through often enough to be worth catching, and
    "follow the Get a vendor approved procedure" is strictly better for the reader.
    """
    names = _key_to_name()
    if not names:
        return text
    return _WF_ID.sub(lambda m: names.get(m.group(0), m.group(0)), text)


# Words the prompt forbids the model to say, mapped to what a colleague would say instead.
#
# Forbidding them was never going to be enough, because our own scaffolding hands them over:
# the user prompt is headed "EXCERPTS FROM THE KNOWLEDGE BASE:", so the model reads the very
# nouns it is told never to use and reaches for them when it needs to refer to its sources.
# You cannot ask a model to unsee its own input. Rewriting deterministically is reliable
# where another instruction is not.
_INTERNAL = [
    (re.compile(r"\bthe excerpts?\b", re.I), "our records"),
    (re.compile(r"\bthese excerpts?\b", re.I), "our records"),
    (re.compile(r"\bthe (?:provided )?context\b", re.I), "our records"),
    (re.compile(r"\bthe knowledge base\b", re.I), "our records"),
    (re.compile(r"\bthe provided documents?\b", re.I), "our records"),
    (re.compile(r"\bthese notes\b", re.I), "our records"),
]


# Longest phrase above is "the provided documents" (22 chars); hold back comfortably more
# so no phrase can straddle the point where we stop buffering.
_TAIL = 40


def strip_internal(text: str) -> str:
    for pattern, replacement in _INTERNAL:
        text = pattern.sub(replacement, text)
    return text


def clean_stream(tokens):
    """Drop a title line the model bolted on, and humanise IDs, as tokens go past.

    Models want to head an answer with the document's name, which reads like a report
    rather than a colleague talking. We buffer only the first line — a few hundred
    milliseconds at most — decide, then stream the rest through untouched.

    Substitutions run over a short trailing window rather than per token. "the excerpts"
    almost never arrives as one token — it comes as "the" then " excerpt" then "s" — so a
    per-token replace sees none of the phrases it is looking for and silently does nothing.
    Holding back the last `_TAIL` characters and cutting at a word boundary lets a phrase
    be matched whole. The cost is that the final few words appear one token later, which is
    imperceptible next to a model emitting at ~12 tokens a second.
    """
    def flush(text):
        return strip_internal(humanise_ids(text))

    buf, decided, carry = "", False, ""
    for tok in tokens:
        if not decided:
            buf += tok
            if "\n" not in buf and len(buf) < 170:
                continue
            decided = True
            head, sep, rest = buf.partition("\n")
            carry = rest.lstrip("\n") if (sep and _TITLE_LINE.match(head)) else buf
        else:
            carry += tok

        if len(carry) > _TAIL:
            cut = carry.rfind(" ", 0, len(carry) - _TAIL)
            if cut > 0:
                emit, carry = carry[:cut], carry[cut:]
                if emit:
                    yield flush(emit)

    tail = carry if decided else buf
    if tail:
        yield flush(tail)
