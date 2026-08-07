"""The answering pipeline: condense → retrieve → decide → generate.

This replaces workflow routing as the primary path. The old design asked a model "which
one workflow answers this?" and then loaded that workflow whole. Two consequences: an
answer could never combine sources, and a routing miss surfaced to the user as a flat
"I don't have this documented". Both are gone.

What happens now:

  1. CONDENSE  a follow-up against the conversation, so "is he single?" is routed as
               "is Yaswanth single?" rather than as three meaningless words.
  2. RETRIEVE  hybrid dense+BM25 over chunks from every workflow, fused by RRF, then
               expanded one hop along the workflow relation graph.
  3. DECIDE    an answering policy from a measured evidence score — answer it, answer
               partially and name the gap, or say warmly that we have nothing.
  4. GENERATE  stream a natural answer under that policy.

The user never sees step 3's reasoning. There is no failure message in this file, because
"retrieval found little" is our problem, not theirs — it changes how we answer, not
whether we do.
"""
import re
import time

from . import chunks as chunkstore
from . import config, db, postprocess, prompts, retrieval
from .providers import get_provider, ProviderError

# Retrieval failing is an internal condition. When it happens we still answer — from the
# conversation, or by asking what they meant — rather than reporting our own plumbing.
POLICIES = {
    "answer": prompts.POLICY_ANSWER,
    "partial": prompts.POLICY_PARTIAL,
    "nothing": prompts.POLICY_NOTHING,
}


def _policy_for(evidence: float) -> str:
    if evidence >= config.EVIDENCE_STRONG:
        return "answer"
    if evidence >= config.EVIDENCE_WEAK:
        return "partial"
    return "nothing"


_HOWTO = re.compile(
    r"\b(how (do|to|can|would|should)|what (are|is) the (steps|process|procedure)"
    r"|walk me through|steps? (to|for)|guide me|show me how|process for|procedure for"
    r"|what do i do|how d(o|oes) (i|we|you))\b", re.I)


def _shape(question, result):
    """Decide the answer's shape here, not in the model.

    A 3B model handed three shape options and asked to pick reliably picks wrong — the
    same question came back as a numbered list once and a wall of prose the next time.
    We already know whether we retrieved ordered steps, so we decide, and hand the model
    one unambiguous instruction. Returns (directive, extra_chunks).
    """
    step_wfs = {}
    for c in result["chunks"]:
        if c["source"].startswith("step-"):
            step_wfs[c["wf_key"]] = step_wfs.get(c["wf_key"], 0) + 1
    procedural = bool(step_wfs) and bool(_HOWTO.search(question))

    if not procedural:
        return ("Answer this as information, not as a procedure: prose, or a few bullets "
                "if there are several distinct facts. Do NOT produce a numbered list of "
                "steps. If there is a cause and a fix, give both."), []

    # Pull the complete procedure for the strongest step-bearing workflow.
    top_wf = max(step_wfs, key=step_wfs.get)
    have = {c["chunk_id"] for c in result["chunks"]}
    extra = [{**c, "chunk_id": c["id"], "score": 0.0, "via": "procedure"}
             for c in chunkstore.steps_for(top_wf) if c["id"] not in have]
    total = step_wfs[top_wf] + len(extra)
    return (f"This is a how-to. Produce ONE numbered list containing all {total} steps, "
            f"numbered 1 to {total}, in order — never restart the numbering, never stop "
            f"early, never merge two steps. Format each item as: **Title.** then your own "
            f"plain-English explanation. Every step whose excerpt carries a line starting "
            f"\"✓\" must keep that line, on its own line, worded as given. Do not answer "
            f"in prose paragraphs."), extra


def _status_line(result, policy):
    """What the UI shows while the model warms up. Describes what we're reading, never
    how the retrieval went."""
    wfs = [w for w in result["workflows"] if w["chunk_count"]]
    if policy == "nothing" or not wfs:
        return "Checking what we have…"
    if len(wfs) == 1:
        return f"Reading “{wfs[0]['name']}”…"
    return f"Pulling together {len(wfs)} sources…"


def _where(anchor: dict, file: str = "") -> str:
    """An anchor as a person would say it: "page 7", "row 12", "04:12–04:38".

    The pipeline records where every piece of text came from — which page of the PDF, which
    paragraph, which spreadsheet row, which seconds of the recording — and until now none of
    it reached the reader. "Based on Yaswanth — CV" is a citation you have to take on trust;
    "page 2, paragraph 14" is one you can check, which is the whole point of citing.
    """
    if not anchor:
        return file or ""
    bits = []
    if "page" in anchor:
        bits.append(f"page {anchor['page']}")
    if "sheet" in anchor:
        bits.append(f"sheet {anchor['sheet']}")
    if "row" in anchor:
        bits.append(f"row {anchor['row']}")
    if "para" in anchor:
        bits.append(f"paragraph {anchor['para']}")
    if "table" in anchor:
        bits.append(f"table {anchor['table']}")
    if "image_index" in anchor:
        bits.append(f"image {anchor['image_index']}")
    if "t_start" in anchor:
        def clock(s):
            s = int(float(s))
            return f"{s // 60}:{s % 60:02d}"
        end = anchor.get("t_end")
        bits.append(f"{clock(anchor['t_start'])}–{clock(end)}" if end
                    else f"at {clock(anchor['t_start'])}")
    located = ", ".join(bits)
    return f"{file} · {located}" if file and located else (located or file)


def _sources(result):
    """Provenance for the answer footer, richest workflow first."""
    by_wf = {}
    for c in result.get("chunks", []):
        where = _where(c.get("anchor") or {}, (c.get("meta") or {}).get("file", ""))
        if where:
            by_wf.setdefault(c["wf_key"], [])
            if where not in by_wf[c["wf_key"]]:
                by_wf[c["wf_key"]].append(where)

    out = []
    for w in result["workflows"]:
        if not w["chunk_count"] and not w["related"]:
            continue
        card = next((c for c in db.get_catalog() if c["wf_key"] == w["wf_key"]), None)
        out.append({
            "wf_key": w["wf_key"], "name": w["name"],
            "owner": (card or {}).get("owner", ""),
            "chunks": w["chunk_count"], "related": w["related"],
            # Where inside the document, when we know. Several entries when an answer drew
            # on several places in the same file — which is worth seeing.
            "locations": by_wf.get(w["wf_key"], [])[:4],
        })
    return out


def _verifications(result):
    """Success-check lines present in the retrieved chunks, so the client can mark them
    even when the model drops the ✓."""
    out = []
    keys = {w["wf_key"] for w in result["workflows"]}
    for card in db.get_catalog():
        if card["wf_key"] not in keys:
            continue
        pkg = db.get_package(card["id"])
        for s in (pkg or {}).get("steps", []):
            v = (s.get("verification") or "").strip()
            if v and v.casefold() != (s.get("body") or "").strip().casefold():
                out.append(v)
    return out


def answer_stream(question: str, profile: str = "a team member", style: str = "",
                  history: list = None, asked_by: str = ""):
    """Yields (event, payload): 'status', 'grounding', 'delta', 'meta'."""
    started = time.time()
    prov = get_provider()

    # Tell the client how long this usually takes before anything slow begins, so it can
    # show a bar that moves instead of a line that doesn't. On a local CPU model the gap
    # between pressing Enter and the first token is tens of seconds, and a caret blinking
    # under "Reading …" for that long reads as a hang rather than as work.
    eta = db.typical_answer_seconds()
    yield ("progress", {"label": "Getting started", "pct": 3, "eta": eta})

    search_q = question
    if history:
        # Condensing is a full model call, so on a local CPU this stage alone can run for
        # twenty seconds. It gets its own label or the reader watches a bar climb under
        # "Getting started" and reasonably concludes nothing is happening.
        yield ("progress", {"label": "Reading the conversation", "pct": 8, "eta": eta})
        try:
            search_q = prov.condense(question, history)
        except ProviderError:
            search_q = question

    yield ("progress", {"label": "Searching your knowledge base", "pct": 20, "eta": eta})

    # Both the literal question and the rewrite are searched — see retrieval.retrieve for
    # why dropping the user's own wording is dangerous.
    result = retrieval.retrieve(question, alt_query=search_q)
    evidence = result["evidence"]
    policy = _policy_for(evidence)

    # Three ways of discovering that the retrieved material is about somebody else. All of
    # them end the same way, because the remedy is the same.
    #
    #   unsupported_terms  nothing in the question appears in the text we retrieved.
    #   unknown_subjects   the question names a person or company the corpus has never
    #                      mentioned — "any info about Sreedhar Masula?".
    #   subject_miss       the question names someone we DO hold, but nothing we retrieved
    #                      is about them.
    #
    # The obvious alternative, keeping the context and instructing the model not to use it,
    # does not work: handed a page of CV facts and asked about availability, it either
    # recited the facts or invented a negative ("is not available at any specific timings").
    # You cannot reliably out-prompt a full context window. Removing the facts removes the
    # temptation, and this path already produces good answers.
    #   thin_match         every word is in the corpus, but no single document covers the
    #                      question — the match was assembled from unrelated sources.
    # attribute_gap is deliberately NOT in this list, though it was for one revision.
    #
    # Routing it here stopped the invention it was aimed at, and broke something worse: the
    # check is lexical, so "may I know Yaswanth's complete profile" and "what technologies
    # can Yaswanth do" both look like gaps — neither word appears in his CV — and both were
    # refused out of a document that answers them completely. Refusing a question we can
    # answer is a worse failure than hedging one we half can, and it is the failure a user
    # notices first.
    #
    # Three signals were measured for telling a synonym from a real gap, and none separates:
    #
    #   dense similarity, whole corpus     answer 0.655-0.833  decline 0.513-0.741
    #   dense similarity, retained chunks  answer 0.544-0.777  decline 0.479-0.742
    #   summary + trigger phrases          covers neither "profile" nor "technologies"
    #
    # A CV is semantically close to a salary question while containing no salary, which is
    # why the dense ranges refuse to come apart. Without a signal that separates them, a
    # threshold here would be a guess, so the gap stays a `partial` answer: say what we do
    # hold, name what is missing, invent nothing.
    if (result.get("unsupported_terms") or result.get("unknown_subjects")
            or result.get("subject_miss") or result.get("thin_match")):
        policy = "nothing"

    # Retrieval is done; everything after this is the model writing. That is the great
    # majority of the wait, so the bar hands the rest of its range to it. The label names
    # what is being read, which is more reassuring than a stage name — it shows the system
    # found something.
    yield ("progress", {"label": _status_line(result, policy).rstrip("…"),
                        "pct": 32, "eta": eta})
    yield ("grounding", {"verifications": _verifications(result)})

    near_wf = None
    directive, extra = "", []
    if policy != "nothing":
        directive, extra = _shape(question, result)
        if extra:
            result["chunks"] = result["chunks"] + extra

    context = retrieval.context_for_prompt(result) if policy != "nothing" else ""
    if policy == "nothing":
        # Give the model the near-misses anyway. It can't answer from them, but it can
        # offer the neighbouring topic instead of a dead end — which is the difference
        # between "we don't have that" and "we don't have that, but I do have X".
        #
        # The names go in too, and they matter more than they look. "I don't have anything
        # on Sreedhar Masula, though I do have Yaswanth Kamineni's CV" is a reply that shows
        # its work: the reader can see we understood who they meant and that we are not
        # confusing him with the person we do hold. A bare "I don't have that" leaves them
        # wondering whether we even parsed the name.
        # One neighbour, not three. Handed a list, the model welds it together: offered
        # "Expense Policy" and "Yaswanth — CV" as alternatives to a question about annual
        # leave, it replied "you might find information on Yaswanth Kamineni's expense
        # policy" — inventing an owner for a company-wide document. A single name cannot be
        # merged with anything.
        #
        # And only when retrieval found something genuinely adjacent. `near` is just
        # whatever ranked highest, which for a question the corpus knows nothing about is
        # the nearest thing in an empty room: asked where to park a bike, it offered "Roll
        # back a production deploy". An irrelevant offer is worse than none — it reads as
        # though we misunderstood the question.
        # A thin match has no neighbour by definition: no document covers the question, so
        # whatever ranked top is adjacent to some of its words, not to what was asked.
        # Offering the VPN runbook to someone asking about GitHub secrets reads as though we
        # misheard them.
        offerable = (evidence >= config.EVIDENCE_WEAK and not result.get("thin_match"))
        near_wf = result["workflows"][0] if result["workflows"] and offerable else None
        near = near_wf["name"] if near_wf else ""
        # Title-cased: these arrive normalised for matching, and a model shown "sreedhar
        # masula" writes it back exactly that way — which reads as though we didn't
        # recognise it as a name.
        def _named(key):
            return ", ".join(s.title() for s in (result.get(key) or []))

        # Phrased as notes in brackets, not as prose. Written as a sentence — "Nothing on
        # file is about: Sreedhar Masula." — the model copied it out verbatim, colon and
        # all, in two runs out of three. It is the same parroting that made an example
        # about watering a plant come back as the answer: anything in the context that
        # reads like a finished sentence is a sentence the model may simply repeat. Notes
        # have to be rewritten to be used.
        #
        # The first line is unconditional, and it has to be. Without it, a question we
        # could not name a subject for arrived with nothing in context but the neighbouring
        # topic — and asked "in what timings is he available?", the model read
        # "(one thing we do hold — Yaswanth — CV)" and replied "Yaswanth Kamineni's CV is
        # available." It answered the word rather than the question, because nothing in
        # front of it said there was no answer.
        lines = ["(nothing on file answers this question)"]

        # Two different situations, and telling them apart matters: one of these names
        # somebody we have never heard of, the other somebody we know well but hold nothing
        # relevant about. Collapsing them — as an earlier version did by falling back from
        # unknown to known subjects — tells the reader we have no material on a colleague
        # whose CV we are holding.
        if result.get("unknown_subjects"):
            lines.append(f"(they asked about — {_named('unknown_subjects')}; "
                         f"no material exists under that name)")
        elif result.get("query_subjects"):
            lines.append(f"(we do hold material on {_named('query_subjects')}, but none of "
                         f"it covers what they asked)")
        # The neighbour goes in whatever else we worked out, because it is the difference
        # between "we don't have that" and "we don't have that, but I do have X" — and the
        # second is the reply that actually helps. An earlier chain offered it only when we
        # had identified no subject at all, which silently removed it from exactly the case
        # it was written for: a named person we hold nothing on.
        if near:
            # "name it exactly" alone produced a bare title on its own line — "Bike
            # parking." — because naming it exactly is easiest to satisfy by naming it and
            # nothing else. The offer has to be a sentence to read as an offer.
            lines.append(f"(one different thing we do hold — {near} — offer it in a full "
                         f"sentence, using that name exactly)")
        context = "\n".join(lines)

    # Naming the absent words is what keeps this honest. Measured over three rounds on "what
    # programming languages does Arjun know", against a rota holding week numbers and names:
    #
    #     no note at all                    invents every time
    #     abstract note ("nothing on file") invented 2 of 2
    #     concrete note (the words listed)  invented 1 of 3
    #
    # One in three is the residual, and it is the model's ceiling rather than the pipeline's:
    # handed material about the right person and a question that material cannot answer, a
    # 3B produces something question-shaped. Removing the material entirely does fix it, and
    # costs more than it saves — see the note on attribute_gap above. A larger model is the
    # real remedy; this note is the mitigation that works without one.
    gap = result.get("attribute_gap") or []
    gap_note = ""
    if gap and policy != "nothing":
        # Concrete and almost clinical, on purpose. Softening this to "Nothing on file
        # mentions: programming, languages" — to avoid handing the model vocabulary the
        # system prompt bans — measurably weakened it: invention went from 0 in 3 runs to 2
        # in 2. Naming the literal words that are absent from the literal text is what the
        # model can act on; an abstract statement about what we hold is not.
        #
        # The banned vocabulary is dealt with where it belongs, in postprocess.strip_internal,
        # which rewrites it deterministically on the way out. Strong instruction to the
        # model, clean wording to the reader — rather than one compromise serving neither.
        gap_note = ("\n\nThe excerpts do not use the words: " + ", ".join(gap) +
                    ". State no value for any of them — no guess, no example, no "
                    "placeholder, and no claim about what is or is not true of them. Say "
                    "what the excerpts DO record about this subject, then that this "
                    "particular part isn't on file.")

    # The last label before the long silence. Everything from here to the first token is
    # the model reading the prompt, which is most of the wait and produces no output.
    yield ("progress", {"label": "Writing the answer", "pct": 45, "eta": eta})

    full = ""
    try:
        policy_text = (POLICIES[policy] + ("\n\n" + directive if directive else "")
                       + gap_note)
        raw = prov.stream_rag(question, context, policy_text,
                              profile=profile, history=history, style=style)
        for tok in postprocess.clean_stream(raw):
            full += tok
            yield ("delta", tok)
    except Exception:
        if not full:
            # Even total failure stays in the user's language, not ours.
            yield ("delta", "I'm having trouble reaching my model right now — "
                            "could you try that again in a moment?")

    if policy == "nothing":
        db.add_gap(question)

    # Abstaining is always low confidence whatever the retrieval score said. The subject
    # rules exist precisely because a decent-looking score can sit on material about the
    # wrong person — 0.412 on the question that started this — so reporting that number as
    # "medium confidence" next to "we don't have anything on that" contradicts the answer.
    band = ("low" if policy == "nothing"
            else "high" if evidence >= 0.6
            else "medium" if evidence >= config.EVIDENCE_STRONG else "low")
    sources = _sources(result) if policy != "nothing" else []
    primary = sources[0] if sources else None

    meta = {
        "abstained": policy == "nothing",
        "band": band,
        "confidence": evidence,
        "provider": prov.name,
        "degraded": prov.degraded_reason,
        "retrieval": {"policy": policy, "chunks": len(result["chunks"]),
                      "semantic": result["dense"],
                      "workflows": [s["wf_key"] for s in sources]},
        "sources": sources,
        "verifications": _verifications(result),
        "followups": _followups(result),
    }
    # On an abstain the neighbouring topic goes out as DATA, not as a sentence we hoped the
    # model would write. Asked to include it, a 3B either omitted it — leaving a bare "we
    # don't have that" and wasting the one useful thing we knew — or, when told to include
    # it every time, produced "Roll back a production deploy." on a line of its own. A chip
    # is deterministic, correctly worded every time, and already how this UI offers a
    # related topic everywhere else.
    if policy == "nothing" and near_wf:
        meta["alternatives"] = [{"wf_key": near_wf["wf_key"], "name": near_wf["name"]}]

    if primary:
        meta["workflow"] = {
            "wf_key": primary["wf_key"], "name": primary["name"],
            "owner": primary["owner"], "category": "", "verified_by": "",
            "verified_at": "", "step_count": None,
        }
        card = next((c for c in db.get_catalog() if c["wf_key"] == primary["wf_key"]), None)
        if card:
            pkg = db.get_package(card["id"])
            if pkg:
                meta["workflow"].update({
                    "category": pkg.get("category", ""),
                    "verified_by": pkg.get("verified_by", ""),
                    "verified_at": pkg.get("verified_at", ""),
                    "step_count": len(pkg.get("steps") or []),
                })
        meta["alternatives"] = [{"wf_key": s["wf_key"], "name": s["name"]}
                                for s in sources[1:3]]

    # Wall-clock for the whole answer. The one number that tells an operator whether this is
    # usable on their hardware — a knowledge base nobody waits for is a knowledge base
    # nobody uses — and it costs a subtraction.
    elapsed = round(time.time() - started, 2)
    meta["elapsed"] = elapsed
    db.log_ask(question, {"policy": policy, "evidence": evidence},
               [s["wf_key"] for s in sources],
               {"streamed": True, "headline": full[:120], "elapsed": elapsed},
               evidence, policy == "nothing", prov.name, asked_by=asked_by)
    yield ("meta", meta)


def answer(question: str, profile: str = "a team member", style: str = "",
           history: list = None, asked_by: str = "") -> dict:
    """Non-streaming answer, for the CLI and /api/ask. Same pipeline, collected."""
    text, meta = "", {}
    for event, payload in answer_stream(question, profile=profile, style=style,
                                        history=history, asked_by=asked_by):
        if event == "delta":
            text += payload
        elif event == "meta":
            meta = payload
    return {"answer": text.strip(), **meta}


def _followups(result, limit=3):
    """Next questions we can actually answer, drawn from the retrieved workflows' FAQs."""
    out, seen = [], set()
    for w in result["workflows"]:
        card = next((c for c in db.get_catalog() if c["wf_key"] == w["wf_key"]), None)
        if not card:
            continue
        pkg = db.get_package(card["id"])
        for f in (pkg or {}).get("faqs", []):
            q = (f.get("question") or "").strip()
            if q and q.lower() not in seen:
                seen.add(q.lower())
                out.append(q)
        for r in (pkg or {}).get("related", []):
            n = (r.get("name") or "").strip()
            if n and n.lower() not in seen:
                seen.add(n.lower())
                out.append(f"How do I {n[0].lower()}{n[1:]}?")
    return out[:limit]
