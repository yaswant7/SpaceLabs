"""The ask pipeline:  route -> scoped retrieve (top-K union) -> compose -> ground & gate.

Two things this file is careful about, both of which the user called out:
  1. It never blindly locks to a single workflow. Routing returns up to 3 candidates;
     depending on scores it answers scoped (1), disambiguates (2), or builds a journey (many).
  2. It refuses to be confidently wrong. Every answer is graded for citation coverage and
     gated by confidence; a weak answer becomes an honest abstention + a knowledge gap.
"""
from . import config, db
from .providers import get_provider, ProviderError


def _valid_cite(cite, wf_keys):
    return bool(cite) and any(str(cite).startswith(k) for k in wf_keys)


def _coverage(blocks, wf_keys):
    """Fraction of claim-bearing blocks that cite a real workflow. Deterministic grounding check."""
    claim, cited = 0, 0
    for b in blocks:
        t = b.get("type")
        if t == "text":
            claim += 1
            if any(_valid_cite(c, wf_keys) for c in b.get("cites", [])):
                cited += 1
        elif t == "steps":
            claim += 1
            if any(_valid_cite(s.get("cite"), wf_keys) for s in b.get("steps", [])):
                cited += 1
        elif t == "known_error":
            claim += 1
            if _valid_cite(b.get("cite"), wf_keys):
                cited += 1
    return (cited / claim) if claim else 1.0


def _abstain(question, headline, provider_name, alternatives=None):
    db.add_gap(question)
    ans = {
        "abstained": True, "band": "low", "confidence": 0.3, "coverage": 0.0,
        "headline": headline, "workflow": None, "blocks": [], "sources": [],
        "alternatives": alternatives or [], "followups": [], "clarify": None,
        "provider": provider_name,
        "escalation": {"note": "Logged as a knowledge gap. A senior can turn this into a workflow."},
    }
    db.log_ask(question, {}, [], ans, 0.3, True, provider_name)
    return ans


def ask(question: str, profile: str = "new team member", style: str = "") -> dict:
    prov = get_provider()
    cards = db.get_catalog()
    if not cards:
        return _abstain(question, "No workflows have been added yet. Ask a senior to feed one in.", prov.name)

    key_to_card = {c["wf_key"]: c for c in cards}

    try:
        route = prov.route(question, cards)
    except ProviderError as e:
        return _abstain(question, f"Routing failed: {e}", prov.name)

    cands = [c for c in route.get("candidates", []) if c.get("wf_key") in key_to_card]

    # exact error-code match forces the owning workflow in as a strong candidate
    ql = question.lower()
    for ke in db.all_known_error_codes():
        code = (ke.get("code") or "").lower()
        if code and code in ql and ke["wf_key"] in key_to_card:
            if ke["wf_key"] not in {c["wf_key"] for c in cands}:
                cands.insert(0, {"wf_key": ke["wf_key"], "score": 0.9, "why": "known error code match"})

    cands.sort(key=lambda c: c.get("score", 0), reverse=True)
    kept = [c for c in cands if c.get("score", 0) >= config.ROUTE_MIN_SCORE]
    if not kept:
        return _abstain(question, "I don't have a documented workflow for this yet.", prov.name)

    top = kept[0]
    margin = top["score"] - (kept[1]["score"] if len(kept) > 1 else 0.0)
    spanning = bool(route.get("spanning"))
    disambiguate = (not spanning) and margin < config.AMBIGUOUS_MARGIN and len(kept) > 1

    # choose which workflows to actually load
    if spanning:
        selected = kept[:3]
    elif disambiguate:
        selected = kept[:2]
    else:
        selected = kept[:1]

    packages, wf_keys, route_scores = [], [], {}
    for c in selected:
        pkg = db.get_package(key_to_card[c["wf_key"]]["id"])
        if pkg:
            packages.append(pkg)
            wf_keys.append(pkg["wf_key"])
            route_scores[pkg["wf_key"]] = c.get("score", 0.6)

    if not packages:
        return _abstain(question, "I couldn't load the matching workflow.", prov.name)

    alt_note = ""
    if len(selected) > 1:
        alt_note = " Alternatives: " + ", ".join(f"{c['wf_key']} ({c.get('score')})" for c in selected[1:])
    routing_note = f"Router chose {top['wf_key']} ({top['score']}).{alt_note} " \
                   f"If the top workflow doesn't contain the answer but another does, say so."

    try:
        ans = prov.compose(question, packages, routing_note=routing_note, profile=profile,
                           route_scores=route_scores, spanning=spanning, style=style)
    except ProviderError as e:
        return _abstain(question, f"Composing failed: {e}", prov.name)

    blocks = ans.get("blocks", [])
    coverage = _coverage(blocks, wf_keys)
    route_conf = top["score"]
    model_conf = float(ans.get("confidence", 0.6) or 0.6)
    confidence = round(min(0.5 * route_conf + 0.4 * model_conf + 0.1 * coverage, 0.99), 3)

    weak = ans.get("abstain") or confidence < config.CONF_LOW or coverage < 0.34
    if weak:
        alt = [{"wf_key": p["wf_key"], "name": p["name"]} for p in packages]
        return _abstain(question,
                        ans.get("headline") or "I'm not confident enough to answer this from what I have.",
                        prov.name, alternatives=alt)

    if disambiguate:
        band = "medium"
    elif confidence >= config.CONF_HIGH and margin >= config.AMBIGUOUS_MARGIN:
        band = "high"
    else:
        band = "medium"

    primary = next((p for p in packages if p["wf_key"] == ans.get("primary_wf_key")), packages[0])
    alternatives = ans.get("alternatives") or [
        {"wf_key": p["wf_key"], "name": p["name"]} for p in packages if p["wf_key"] != primary["wf_key"]]

    result = {
        "abstained": False,
        "band": band,
        "confidence": confidence,
        "coverage": round(coverage, 2),
        "headline": ans.get("headline", ""),
        "workflow": {
            "wf_key": primary["wf_key"], "name": primary["name"], "owner": primary["owner"],
            "version": primary["version"], "verified_by": primary["verified_by"],
            "verified_at": primary["verified_at"], "category": primary["category"],
        },
        "spanning": spanning,
        "blocks": blocks,
        "sources": ans.get("sources", []),
        "alternatives": alternatives,
        "followups": ans.get("followups", []),
        "clarify": route.get("clarify") if disambiguate else None,
        "provider": prov.name,
    }
    db.log_ask(question, route, wf_keys, result, confidence, False, prov.name)
    return result


def ask_stream(question: str, profile: str = "new team member", style: str = ""):
    """Streaming variant: route → retrieve → stream the composed markdown live.

    Yields (event, payload) tuples: 'status' (progress line), 'delta' (a token),
    'meta' (final source/band/alternatives). Grounding is enforced by the compose prompt;
    the rigorous citation-coverage grading lives on the non-streaming /api/ask path."""
    prov = get_provider()
    cards = db.get_catalog()
    if not cards:
        yield ("delta", "No workflows have been added yet — ask a senior to add one.")
        yield ("meta", {"abstained": True, "provider": prov.name})
        return

    key_to_card = {c["wf_key"]: c for c in cards}
    try:
        route = prov.route(question, cards)
    except ProviderError:
        route = {"candidates": [], "out_of_scope": True, "spanning": False}

    cands = [c for c in route.get("candidates", []) if c.get("wf_key") in key_to_card]
    ql = question.lower()
    for ke in db.all_known_error_codes():
        code = (ke.get("code") or "").lower()
        if code and code in ql and ke["wf_key"] in key_to_card and ke["wf_key"] not in {c["wf_key"] for c in cands}:
            cands.insert(0, {"wf_key": ke["wf_key"], "score": 0.9})
    cands.sort(key=lambda c: c.get("score", 0), reverse=True)
    kept = [c for c in cands if c.get("score", 0) >= config.ROUTE_MIN_SCORE]

    if not kept:
        db.add_gap(question)
        yield ("delta", "I don't have this documented yet — I've flagged it so a senior can add it. "
                        "You'll be covered next time.")
        yield ("meta", {"abstained": True, "provider": prov.name})
        return

    top = kept[0]
    margin = top["score"] - (kept[1]["score"] if len(kept) > 1 else 0.0)
    spanning = bool(route.get("spanning"))
    disambiguate = (not spanning) and margin < config.AMBIGUOUS_MARGIN and len(kept) > 1
    selected = kept[:3] if spanning else (kept[:2] if disambiguate else kept[:1])

    packages = []
    for c in selected:
        pkg = db.get_package(key_to_card[c["wf_key"]]["id"])
        if pkg:
            packages.append(pkg)
    if not packages:
        yield ("delta", "I couldn't load the matching workflow.")
        yield ("meta", {"abstained": True, "provider": prov.name})
        return

    yield ("status", f"Reading “{packages[0]['name']}”…")
    routing_note = f"Router chose {top['wf_key']}. If it doesn't contain the answer but " \
                   f"another provided package does, say so."

    full = ""
    try:
        for tok in prov.stream_markdown(question, packages, routing_note=routing_note,
                                        profile=profile, style=style, spanning=spanning):
            full += tok
            yield ("delta", tok)
    except Exception:
        if not full:
            yield ("delta", "Sorry — I hit an error generating that. Please try again.")

    route_conf = top["score"]
    confidence = round(min(0.55 + route_conf * 0.4, 0.98), 2)
    band = "high" if (confidence >= config.CONF_HIGH and margin >= config.AMBIGUOUS_MARGIN
                      and not disambiguate) else "medium"
    primary = packages[0]
    meta = {
        "abstained": False, "band": band, "confidence": confidence,
        "workflow": {"wf_key": primary["wf_key"], "name": primary["name"], "owner": primary["owner"],
                     "verified_by": primary["verified_by"], "verified_at": primary["verified_at"]},
        "alternatives": [{"wf_key": p["wf_key"], "name": p["name"]} for p in packages[1:]],
        "provider": prov.name,
    }
    db.log_ask(question, route, [p["wf_key"] for p in packages],
               {"headline": full[:120], "streamed": True}, confidence, False, prov.name)
    yield ("meta", meta)
