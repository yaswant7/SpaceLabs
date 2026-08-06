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


def clean_stream(tokens):
    """Drop a title line the model bolted on, and humanise IDs, as tokens go past.

    Models want to head an answer with the document's name, which reads like a report
    rather than a colleague talking. We buffer only the first line — a few hundred
    milliseconds at most — decide, then stream the rest through untouched.

    ID substitution is applied per token boundary rather than to the whole line, so it
    never delays output; a key split across two tokens is missed, which is an acceptable
    trade for zero added latency.
    """
    buf, decided = "", False
    for tok in tokens:
        if decided:
            yield humanise_ids(tok)
            continue
        buf += tok
        if "\n" not in buf and len(buf) < 170:
            continue
        decided = True
        head, sep, rest = buf.partition("\n")
        if sep and _TITLE_LINE.match(head):
            rest = rest.lstrip("\n")
            if rest:
                yield humanise_ids(rest)
        else:
            yield humanise_ids(buf)
    if not decided and buf:
        yield humanise_ids(buf)
