"""Hybrid retrieval over chunks — dense + lexical, fused, then expanded across workflows.

Why hybrid rather than just embeddings: dense search generalises ("undo a release" finds
a rollback runbook) but blurs rare literals, and enterprise questions are full of rare
literals — ERR_LEASE_HELD, `spacectl`, JNTUK, a person's name. BM25 is exact on precisely
those and hopeless at paraphrase. Each covers the other's failure, so we run both and
fuse. When the embedder is unavailable the dense leg simply returns nothing and BM25
carries the request; retrieval degrades, it never breaks.

Fusion is Reciprocal Rank Fusion. RRF combines by *rank*, not score, which matters because
a cosine similarity and a BM25 score are not on comparable scales and any attempt to
normalise them into one number is a fudge that needs retuning whenever the corpus changes.

Two things happen after fusion, and they are what make retrieval workflow-aware rather
than workflow-bound:

  * chunks are grouped by workflow, so one answer can legitimately draw on several
  * the workflow relation graph is walked one hop, so a workflow that says "you must do X
    first" pulls X's context in even when the question never mentioned it

Scaling: `search_dense` is brute force over a float32 matrix, which is exact and fast to
~10^5 chunks. Beyond that it becomes an ANN index — see docs/ARCHITECTURE-RAG.md. Nothing
in this module's interface changes when that happens.
"""
import math
import re
import time

from . import chunks as chunkstore
from . import config, db, subjects

_WORD = re.compile(r"[a-z0-9][a-z0-9+#.\-_]*")

_STOP = {
    "the", "a", "an", "and", "or", "but", "if", "of", "to", "in", "on", "for", "with",
    "at", "by", "from", "about", "into", "over", "after", "is", "are", "was", "were",
    "be", "been", "being", "have", "has", "had", "do", "does", "did", "will", "would",
    "should", "can", "could", "may", "might", "must", "i", "you", "he", "she", "it",
    "we", "they", "me", "him", "her", "us", "them", "my", "your", "his", "its", "our",
    "their", "this", "that", "these", "those", "what", "which", "who", "whom", "when",
    "where", "why", "how", "all", "any", "some", "such", "no", "not", "only", "so",
    "than", "too", "very", "just", "get", "got", "need", "know", "tell", "please",
    "there", "here", "also", "more", "most", "much", "many", "one", "two", "up", "out",
    # Fillers and light verbs. These carry no retrieval signal but are rare enough in a
    # small corpus to look "distinctive" to IDF — which made the unsupported-term guard
    # flag "anything" in "know anything about X" and penalise a perfectly good answer.
    "anything", "something", "everything", "anyone", "someone", "everyone", "anybody",
    "somebody", "want", "like", "make", "made", "give", "given", "find", "help", "use",
    "using", "used", "see", "let", "say", "said", "think", "really", "actually", "maybe",
    "thanks", "thank", "okay", "yes", "no", "now", "then", "still", "even", "back",
    "does", "done", "go", "goes", "going", "come", "take", "put", "keep", "look",
    "work", "works", "thing", "things", "way", "ways", "able", "possible", "new", "old",
}

_K1, _B = 1.4, 0.72
_lex = {"at": 0.0, "n": -1, "docs": None, "idf": None, "avg": 1.0}
_LEX_TTL = 20.0


def tokens(text: str) -> list:
    return [t for t in _WORD.findall((text or "").lower())
            if len(t) > 1 and t not in _STOP]


def _lexical_index():
    rows = chunkstore.all_chunks()
    now = time.time()
    if (_lex["docs"] is not None and _lex["n"] == len(rows)
            and now - _lex["at"] < _LEX_TTL):
        return _lex

    docs = []
    for r in rows:
        toks = tokens(f"{r['heading']} {r['text']}")
        if not toks:
            continue
        tf = {}
        for t in toks:
            tf[t] = tf.get(t, 0) + 1
        docs.append({"id": r["id"], "tf": tf, "len": len(toks)})

    df = {}
    for d in docs:
        for t in d["tf"]:
            df[t] = df.get(t, 0) + 1
    n = len(docs) or 1
    _lex.update({
        "at": now, "n": len(rows), "docs": docs,
        "idf": {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()},
        "avg": (sum(d["len"] for d in docs) / n) if docs else 1.0,
    })
    return _lex


_subj = {"at": 0.0, "n": -1, "by_wf": None, "gate": None, "per_chunk": None}


def _subject_index():
    """Who each chunk is about, built over the whole corpus and cached like the BM25 index.

    Deriving this at index time rather than storing it keeps it honest: subjects are decided
    from corpus-wide capitalisation statistics, so a name only becomes a name once the rest
    of the corpus provides the counter-evidence that it isn't an ordinary word. Storing a
    per-chunk value written before its neighbours existed would go stale the moment the next
    document landed.

    It shares the full-corpus scan and TTL that `_lexical_index` already performs, so it adds
    no new scaling limit — both hit the same ~10^5-chunk ceiling, and both are replaced by a
    precomputed index at the same point (docs/ARCHITECTURE-RAG.md).
    """
    rows = chunkstore.all_chunks()
    now = time.time()
    if (_subj["by_wf"] is not None and _subj["n"] == len(rows)
            and now - _subj["at"] < _LEX_TTL):
        return _subj

    cards = db.get_catalog()
    names = {c["wf_key"]: c["name"] for c in cards}
    summaries = {c["wf_key"]: c.get("summary", "") for c in cards}

    # Subjects the ingestion model named when it read the document. These are the portable
    # signal: they need no tuning, they work in whatever language the document is written
    # in, and they can name a subject mentioned only once — all three things the
    # capitalisation statistics cannot do. The statistics stay as the fallback for material
    # ingested before this existed, or when a model returns nothing usable.
    declared = {}
    for c in cards:
        pkg = db.get_package(c["id"])
        if pkg and pkg.get("subjects"):
            declared[c["wf_key"]] = pkg["subjects"]

    grouped, order = {}, []
    for r in rows:
        if r["wf_key"] not in grouped:
            grouped[r["wf_key"]] = []
            order.append(r["wf_key"])
        grouped[r["wf_key"]].append(r)

    packages = [({"wf_key": k, "name": names.get(k, k), "summary": summaries.get(k, ""),
                  "subjects": declared.get(k, [])},
                 [r["text"] for r in grouped[k]]) for k in order]
    idx = subjects.index(packages)

    per_chunk = {}
    for k in order:
        for row, subs in zip(grouped[k], idx[k]["per_text"]):
            per_chunk[row["id"]] = subs

    _subj.update({
        "at": now, "n": len(rows),
        "by_wf": {k: v["workflow"] for k, v in idx.items()},
        "gate": subjects.discriminating({k: v["workflow"] for k, v in idx.items()}),
        "per_chunk": per_chunk,
    })
    return _subj


def invalidate():
    _lex.update({"at": 0.0, "n": -1, "docs": None})
    _subj.update({"at": 0.0, "n": -1, "by_wf": None, "gate": None, "per_chunk": None})
    chunkstore.invalidate()


def search_lexical(query: str, limit: int = None) -> list:
    limit = limit or config.RETRIEVE_CANDIDATES
    idx = _lexical_index()
    if not idx["docs"]:
        return []
    q = set(tokens(query))
    if not q:
        return []
    avg = idx["avg"] or 1.0
    hits = []
    for d in idx["docs"]:
        score = 0.0
        for t in q:
            f = d["tf"].get(t)
            if not f:
                continue
            score += idx["idf"].get(t, 0.0) * (f * (_K1 + 1)) / (
                f + _K1 * (1 - _B + _B * d["len"] / avg))
        if score > 0:
            hits.append({"chunk_id": d["id"], "score": round(score, 4)})
    hits.sort(key=lambda h: h["score"], reverse=True)
    return hits[:limit]


def _rrf(rankings: list) -> dict:
    """Reciprocal Rank Fusion: score = Σ weight / (k + rank).

    Takes a list of (weight, hits) so several legs can share a weight — with a dict keyed
    by weight, two retrievers at the same weight silently overwrote each other.
    """
    fused = {}
    for weight, hits in rankings:
        for rank, h in enumerate(hits, 1):
            fused[h["chunk_id"]] = fused.get(h["chunk_id"], 0.0) + weight / (config.RRF_K + rank)
    return fused


def _relation_neighbours(wf_keys: set) -> set:
    """One hop across the workflow graph.

    A procurement order that names vendor approval as a prerequisite should be able to
    answer "can I raise a PO for a new supplier?" using both. One hop, not transitive
    closure — two hops out, relevance decays faster than the extra context helps.
    """
    if not wf_keys:
        return set()
    conn = db.connect()
    q = ",".join("?" * len(wf_keys))
    rows = conn.execute(
        f"SELECT w1.wf_key AS a, w2.wf_key AS b FROM relations r "
        f"JOIN workflows w1 ON w1.id = r.from_id "
        f"JOIN workflows w2 ON w2.id = r.to_id "
        f"WHERE w1.wf_key IN ({q}) OR w2.wf_key IN ({q})",
        (*wf_keys, *wf_keys)).fetchall()
    conn.close()
    out = set()
    for r in rows:
        out.add(r["a"])
        out.add(r["b"])
    return out - wf_keys


def retrieve(query: str, alt_query: str = None, expand_relations: bool = True) -> dict:
    """The main entry point.

    `query` is what the user actually typed. `alt_query` is the condensed rewrite, when
    there is one, and BOTH are searched.

    Searching only the rewrite caused a real conflation. Asked "you know anything about
    yaswanth?" during a conversation about purchase orders, the condenser produced "Can a
    purchase order be raised for an unapproved new supplier?" — dropping the person's name
    entirely. Retrieval returned vendor documents, and the model bridged the gap by
    inventing that Yaswanth was a vendor pending approval. Fusing both queries means a
    named entity in the user's own words can never be lost by a rewrite, while a genuine
    follow-up ("what about staging?") still gets the context it needs from the rewrite.

    Returns:
      {
        "chunks":    [{chunk_id, wf_key, heading, source, text, score, via}],
        "workflows": [{wf_key, name, score, chunk_count, related}],
        "evidence":  0.0-1.0   how much we actually found
        "dense":     bool      whether semantic search contributed
      }
    """
    dense = chunkstore.search_dense(query)
    lexical = search_lexical(query)

    rankings = [(1.0, dense), (0.85, lexical)]
    if alt_query and alt_query.strip().casefold() != query.strip().casefold():
        alt_dense = chunkstore.search_dense(alt_query)
        alt_lex = search_lexical(alt_query)
        # Slightly below the literal question: the rewrite is a heuristic, the user's own
        # words are ground truth about what they asked.
        rankings += [(0.9, alt_dense), (0.75, alt_lex)]
        dense = dense + alt_dense
        lexical = lexical + alt_lex

    if not dense and not lexical:
        return {"chunks": [], "workflows": [], "evidence": 0.0, "dense": False,
                "unsupported_terms": [], "query_subjects": [],
                "subject_miss": False, "unknown_subjects": []}

    fused = _rrf([r for r in rankings if r[1]])

    # Subject awareness. Chunks carry who they are ABOUT, and a question that names someone
    # prefers material about that person over material that merely reads alike.
    #
    # The boost must only ever REORDER, never remove. Subjects are extracted statistically,
    # and a heuristic allowed to drop evidence turns a wrong answer into a missing one —
    # which is worse, because nobody reports it as a bug.
    #
    # Enforcing that turned out to need a structural split rather than a careful constant.
    # Two separate mechanisms quietly convert a boost into a filter: the relative cutoff is
    # measured from the top score, so promoting the winner raises the floor under everything
    # else, and the chunk quota is a hard count, so a promoted document simply fills it
    # first. Measured on "is yaswanth on call this week", the boost took the on-call rota
    # from three chunks to zero and left only his CV — deleting the one document that could
    # answer the question, in the name of finding the right person.
    #
    # So admission and ordering are decided by different scores. WHICH chunks are retrieved
    # uses the unboosted ranking, exactly as it did before subjects existed. WHAT ORDER they
    # reach the model in uses the boost. Ordering is where the value was anyway: the model
    # weights the top of its context heavily, so the right person's excerpts leading is most
    # of the benefit, and the abstain rules cover the rest.
    sidx = _subject_index()
    q_subjects = subjects.mentions(query, sidx["gate"])
    if alt_query:
        q_subjects |= subjects.mentions(alt_query, sidx["gate"])

    boosted = dict(fused)
    if q_subjects:
        per_chunk = sidx["per_chunk"]
        for cid in boosted:
            if q_subjects.intersection(per_chunk.get(cid) or ()):
                boosted[cid] *= 1.0 + config.SUBJECT_BOOST

    ranked = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)

    top_ids = [cid for cid, _ in ranked[:config.RETRIEVE_CHUNKS * 2]]
    meta = chunkstore.get_many(top_ids)
    dense_ids = {h["chunk_id"] for h in dense}
    lex_ids = {h["chunk_id"] for h in lexical}

    # Relative cutoff. Taking a fixed top-K always fills the quota, so a question with one
    # strong source gets padded out with weak ones from unrelated documents — and a model
    # handed a CV and an on-call rota together will cheerfully report that the person in
    # the CV is on call. Keeping only chunks within a fraction of the best score means a
    # focused question yields focused context, while a genuinely cross-document question
    # still returns several sources because they all score well.
    top_score = ranked[0][1] if ranked else 0.0
    floor = top_score * config.RELATIVE_CUTOFF

    picked = []
    for cid, score in ranked:
        row = meta.get(cid)
        if not row:
            continue
        if picked and score < floor:
            break
        via = ("both" if cid in dense_ids and cid in lex_ids
               else "semantic" if cid in dense_ids else "keyword")
        picked.append({**row, "chunk_id": cid, "score": round(boosted[cid], 5), "via": via,
                       "subjects": sidx["per_chunk"].get(cid) or []})
        if len(picked) >= config.RETRIEVE_CHUNKS:
            break

    # Admission is settled; now let the subject boost decide what the model reads first.
    picked.sort(key=lambda c: c["score"], reverse=True)

    # When we hold material about the person asked for, material about a DIFFERENT named
    # person is a distractor and is removed.
    #
    # This is the one place subjects filter rather than rank, and it is here because
    # ranking and labelling both proved insufficient. Asked "what is Priya's current role"
    # — Priya being a name in the on-call rota — retrieval correctly returned the rota, and
    # alongside it Yaswanth's CV, which matches "current role" far better than any rota row
    # does. With both in context, clearly labelled, and a prompt rule forbidding exactly
    # this, the model still answered "Priya's current role is Full Stack Engineer". Same
    # for "what programming languages does Arjun know" and "where did Meena study": his
    # skills, his university, somebody else's name on them.
    #
    # You cannot out-label a document that answers the question better than the right one
    # does. The only reliable fix is that it is not there.
    #
    # The precondition keeps this narrow and safe. It fires only when some retrieved chunk
    # IS about the subject asked for — so we are choosing between candidates, never
    # deleting our only evidence. Ask "which vendors has Meena approved" and nothing
    # retrieved is labelled Meena; the filter stays inactive and her vendor list is
    # answered from as before. Chunks carrying no subjects at all are never dropped.
    if q_subjects and any(q_subjects.intersection(c.get("subjects") or ()) for c in picked):
        kept = [c for c in picked
                if not c.get("subjects")
                or q_subjects.intersection(c["subjects"])]
        if kept and len(kept) != len(picked):
            picked = kept
            # Evidence must describe what SURVIVED, not what we just removed. Left
            # unfiltered, the score still reflected the CV's strong lexical and semantic
            # hits while the context held only a rota — so "what programming languages does
            # Arjun know" scored high enough to answer confidently from material that lists
            # no languages at all, and the model obliged with "Python and Java". Dropping
            # evidence along with the chunks it came from is what turns that into "we don't
            # have anything on that".
            kept_ids = {c["chunk_id"] for c in picked}
            dense = [h for h in dense if h["chunk_id"] in kept_ids]
            lexical = [h for h in lexical if h["chunk_id"] in kept_ids]

    # group into workflows, strongest first
    groups = {}
    for c in picked:
        g = groups.setdefault(c["wf_key"], {"wf_key": c["wf_key"], "score": 0.0,
                                            "chunk_count": 0, "related": False})
        g["score"] += c["score"]
        g["chunk_count"] += 1

    if expand_relations:
        for key in _relation_neighbours(set(groups)):
            if key not in groups and len(groups) < config.MAX_WORKFLOWS_IN_ANSWER:
                groups[key] = {"wf_key": key, "score": 0.0, "chunk_count": 0,
                               "related": True}

    names = {c["wf_key"]: c["name"] for c in db.get_catalog()}
    workflows = sorted(groups.values(), key=lambda g: g["score"], reverse=True)
    workflows = workflows[:config.MAX_WORKFLOWS_IN_ANSWER]
    for w in workflows:
        w["name"] = names.get(w["wf_key"], w["wf_key"])
        w["score"] = round(w["score"], 5)

    evidence = _evidence(picked, dense, lexical)
    missing = _unsupported_terms(query, picked)
    if missing:
        # The question names something the retrieved text never mentions. That is exactly
        # the setup for a conflation — the model is handed a subject it was asked about
        # and facts about a different one, and bridges them. Cap the evidence so the
        # answering policy drops to "partial", which tells the model to say what is and
        # isn't on file rather than answering confidently.
        evidence = min(evidence, config.EVIDENCE_STRONG - 0.01)

    # We have the right person and not the right fact. Same cap: answer partially, name the
    # gap, invent nothing. Not "nothing" — abstaining outright would throw away material
    # that genuinely is about who they asked.
    attribute_gap = _attribute_miss(query, picked, q_subjects)
    if attribute_gap:
        evidence = min(evidence, config.EVIDENCE_STRONG - 0.01)

    # Asked about someone we do hold, and yet nothing we retrieved is about them. The boost
    # above already gave every chunk on that subject its best chance to rank, so this is
    # usually the setup for a conflation: material about somebody else, in front of a
    # question about somebody.
    #
    # Usually — not always, and the exception has to be respected or the guard does more
    # harm than the bug. Subject labels are incomplete by construction: part heuristic,
    # part a model that answers differently on different runs. Asked "which vendors has
    # Meena approved", retrieval returned her vendor list at evidence 0.628 — strong,
    # unambiguous, exactly right — and the guard refused it, because that document's labels
    # happened to name the three vendors and not their owner. A knowledge base that
    # declines a question it can plainly answer is worse than one that occasionally
    # over-reaches, because the user has no way to tell it is wrong.
    #
    # So a label mismatch only counts when the retrievers are not confident on their own.
    # Dense and BM25 agreeing strongly is direct evidence about this question; a missing
    # label is an inference from metadata we know to be patchy. The direct signal wins.
    subject_miss = (bool(q_subjects)
                    and evidence < config.EVIDENCE_STRONG
                    and not any(q_subjects.intersection(c.get("subjects") or ())
                                for c in picked))

    # No single document covers enough of the question. Every word may be in the corpus and
    # every retriever may agree — but if the match is assembled from unrelated documents,
    # we do not have this and must not answer it.
    coverage = _best_coverage(query, picked)
    thin_match = bool(picked) and coverage < config.MIN_QUERY_COVERAGE

    vocab = set(_lexical_index()["idf"] or {})
    vocab_stems = {_stem(t) for t in vocab}
    unknown = subjects.unknown_runs(
        query, lambda t: t in vocab or _stem(t) in vocab_stems)

    return {"chunks": picked, "workflows": workflows,
            "evidence": evidence, "dense": bool(dense),
            "unsupported_terms": missing,
            "query_subjects": sorted(q_subjects),
            "subject_miss": subject_miss,
            "attribute_gap": attribute_gap,
            "coverage": coverage,
            "thin_match": thin_match,
            "unknown_subjects": unknown}


def _stem(t: str) -> str:
    """Crude suffix stripping, enough to match study/studied/studying."""
    for suf in ("ings", "ing", "ies", "ied", "es", "ed", "s"):
        if len(t) > len(suf) + 3 and t.endswith(suf):
            return t[:-len(suf)]
    return t


def _unsupported_terms(query: str, picked: list) -> list:
    """The question's content words, when NOT ONE of them appears in the retrieved text.

    This is the "the context isn't about what they asked" detector, and it is deliberately
    the most conservative form of that test. An earlier version scored terms by IDF and
    fired when the rare ones were missing; it was too brittle. On a small corpus the
    entity being asked about can itself fall below the rarity cutoff — "yaswanth" appears
    in enough chunks to look common — leaving the guard to judge on incidental words like
    "profile" and downgrade a perfectly good answer.

    Zero overlap is unambiguous and needs no tuning: if nothing the user asked about is
    mentioned anywhere in what we retrieved, we are about to answer from material on a
    different subject. That is the conflation shape, and nothing else is.
    """
    if not picked:
        return []
    q = [t for t in dict.fromkeys(tokens(query))]
    if not q:
        return []
    seen = {_stem(t) for t in tokens(" ".join(c["text"] for c in picked))}
    missing = [t for t in q if _stem(t) not in seen]
    return missing if len(missing) == len(q) else []


def _best_coverage(query: str, picked: list) -> float:
    """How much of the question the single best-covering document accounts for.

    The last of the "we only appear to have this" checks, and the one that catches what the
    others structurally cannot. `_unsupported_terms` needs EVERY word missing;
    `unknown_runs` needs an unrecognised name; `_attribute_miss` needs a known subject.
    A question can fail all three and still be uncovered, because its words are scattered
    across documents that have nothing to do with each other.

    That is a real answer this produced: "how to read github secrets of a project I've
    access" scored 0.549 and came back with VPN and AWS steps. `github` was in the corpus —
    in somebody's CV. `access` matched two runbooks. `project` matched the CV again. Every
    retriever found something, they agreed with each other, and agreement is what the
    evidence score rewards. No document was about GitHub secrets.

    Grouping by document is the whole point. A question answered by one coherent source
    scores high here; a question answered only by stitching fragments from unrelated
    sources scores low, however well its individual words match.
    """
    terms = {_stem(t) for t in tokens(query)}
    if not terms or not picked:
        return 0.0
    by_wf = {}
    for c in picked:
        by_wf.setdefault(c["wf_key"], []).append(c["text"])
    best = 0.0
    for texts in by_wf.values():
        seen = {_stem(t) for t in tokens(" ".join(texts))}
        best = max(best, len(terms & seen) / len(terms))
    return round(best, 3)


# Words that ask for the document itself rather than for a fact inside it.
#
# "May I know the complete profile of Yaswanth" leaves `complete` and `profile` once his
# name is removed, neither appears anywhere in his CV, and the attribute check therefore
# read a CV as failing to answer a request for a CV — capping the score, hedging the reply
# and stamping it "low confidence". Meanwhile "anything about Yaswanth" answered in full,
# because "anything" happens to be a stopword. The difference was which filler the person
# typed, which is not a difference at all.
#
# These are the words people reach for when they want the whole picture. If nothing but
# these is left, the question IS "tell me about this subject", and the document we hold
# about that subject answers it by definition.
_BROAD_ASK = {
    "profile", "overview", "summary", "summarise", "summarize", "detail", "details",
    "info", "information", "background", "bio", "biography", "introduction", "intro",
    "complete", "full", "entire", "whole", "general", "brief", "everything", "anything",
    "something", "stuff", "about", "regarding", "concerning", "history", "record",
    "records", "profile's", "description", "describe", "elaborate", "explain",
}


def _attribute_miss(query: str, picked: list, q_subjects: set) -> list:
    """Right person, wrong question: we hold material about them, but not about this.

    The sibling of `_unsupported_terms`, and the case that rule cannot see. It fires only
    when NOTHING in the question appears in the retrieved text, so a question that names
    someone we do hold always has at least that name matching — and the rest of the
    question goes unchecked.

    That gap is worth an example. Arjun appears in the on-call rota and nowhere else. Asked
    "what programming languages does Arjun know", retrieval returns his rota rows, the
    lexical hit on his name alone scores high enough to answer confidently, and the model —
    holding a spreadsheet of week numbers and names — replied "Arjun knows Python and
    Java". Neither word is anywhere in the corpus.

    So the subject is checked separately from what was asked about it. When the subject
    matches and every other content word is absent, we have the right document and not the
    right fact. That is the "partial" case by definition: say what the material does cover,
    say plainly that this part isn't on file, and invent nothing.

    Returns the words that are missing rather than a flag, because naming them is what
    finally stopped the invention. A general rule ("never supply the missing part") did
    not — the model wrote "Arjun knows Python and Java" underneath it. Handed the specific
    absence, it has something concrete to refuse.
    """
    if not picked or not q_subjects:
        return []
    rest = [t for t in dict.fromkeys(tokens(query))
            if t not in q_subjects and t not in _BROAD_ASK]
    if not rest:
        return []           # they asked only the name — that IS answerable from the file
    seen = {_stem(t) for t in tokens(" ".join(c["text"] for c in picked))}
    return rest if all(_stem(t) not in seen for t in rest) else []


def _evidence(picked, dense, lexical) -> float:
    """How much we actually found, on a 0-1 scale the answering policy gates on.

    Not the fused RRF score: RRF values are tiny and depend on how many retrievers ran,
    so they say nothing about absolute quality.

    The weights are measured, not guessed. Probing questions we know the corpus can and
    cannot answer (scripts in the repo notes) gave:

        answerable      dense 0.642 – 0.862    bm25 3.23 – 12.05    overlap 2 – 8
        not answerable  dense 0.424 – 0.651    bm25 0.00 (mostly)   overlap 0

    Two things follow. Dense similarity alone CANNOT separate them — the ranges overlap
    at 0.64/0.65 — so anything gating on cosine alone will confidently answer questions
    about the office wifi password out of a deploy runbook. And BM25 presence is the real
    discriminator: every answerable question had a lexical hit, and every unanswerable one
    scored a flat zero. So lexical carries the most weight, with dense and cross-retriever
    agreement as corroboration.

    The interesting middle case is "what is yaswanth's blood group" (bm25 2.31, dense
    0.651): we hold a document about that person but not that fact. It lands mid-band on
    purpose — the right answer is to retrieve the CV and let the composer say what the
    document does and doesn't cover, not to abstain and not to invent.
    """
    if not picked:
        return 0.0
    best_dense = max((h["score"] for h in dense), default=0.0)
    best_lex = max((h["score"] for h in lexical), default=0.0)

    lex_norm = best_lex / (best_lex + 6.0)
    dense_norm = max(0.0, min(1.0, (best_dense - 0.42) / 0.46))   # 0.42→0, 0.88→1
    both = sum(1 for c in picked if c["via"] == "both")
    agreement = min(both, 4) / 4.0

    return round(min(1.0, 0.45 * lex_norm + 0.30 * dense_norm + 0.25 * agreement), 3)


def context_for_prompt(result: dict, budget: int = 9000) -> str:
    """Retrieved chunks as prompt text, grouped by workflow.

    Ordering within a group is the subtle part, and getting it wrong produced a real wrong
    answer. Sorting everything by document order buried the best-matching chunk beneath
    its near-misses: asked who was on call in week W32, the model read W31's row first —
    same shape, same columns, adjacent in the sheet — and answered from that. Models
    weight the top of a context window heavily, so the best evidence has to sit there.

    Steps are the exception. A procedure only makes sense in sequence, so step chunks keep
    document order; everything else is ordered by relevance, best first.
    """
    by_wf = {}
    for c in result.get("chunks", []):
        by_wf.setdefault(c["wf_key"], []).append(c)

    names = {c["wf_key"]: c["name"] for c in db.get_catalog()}
    parts, used = [], 0
    for wf in result.get("workflows", []):
        items = by_wf.get(wf["wf_key"])
        if not items:
            continue
        steps = [c for c in items if c["source"].startswith("step-")]
        facts = [c for c in items if not c["source"].startswith("step-")]
        ordered = (sorted(facts, key=lambda x: x["score"], reverse=True)
                   + sorted(steps, key=lambda x: x["ordinal"]))

        head = f"### {names.get(wf['wf_key'], wf['wf_key'])}  [{wf['wf_key']}]"
        about = _about(items)
        if about:
            head += f"\nThese excerpts are about: {about}"
        parts.append(head)
        used += len(head)
        for c in ordered:
            body = c["text"].strip()
            if used + len(body) > budget:
                break
            # Say when an excerpt is a table row. Pipe-separated cells read as prose to a
            # model, which then narrates them as a sentence and loses which value belonged
            # to which column. Naming the shape costs four words and stops that.
            kind = (c.get("meta") or {}).get("kind", "")
            label = " (one row from a table)" if kind in ("table", "table_row", "sheet") else ""
            parts.append(f"[{c['source']}{label}] {body}")
            used += len(body)
        parts.append("")
    return "\n".join(parts).strip()


def _about(items: list) -> str:
    """The subjects of one workflow's excerpts, as a phrase for the prompt.

    An answer may legitimately draw on several documents at once, and when it does the model
    needs to see where one subject ends and the next begins. Without that boundary a context
    holding a CV and an on-call rota reads as one undifferentiated pile of facts about
    people, and the model reports the person in the CV as being on call — which is exactly
    what it did.

    Ordered by how many of the excerpts each name appears in, and truncated. A CV names its
    owner in nearly every chunk and a technology in one or two, so frequency puts the actual
    subject first — where an unordered list read "about: Kamineni, Yaswanth, Angular, ASP,
    Core, NET" and buried the only name that mattered among the skills section.

    Capitalised because these are names and the model echoes the casing it is shown.
    """
    freq = {}
    for c in items:
        for s in c.get("subjects") or []:
            freq[s] = freq.get(s, 0) + 1
    ranked = sorted(freq, key=lambda s: (-freq[s], s))
    return ", ".join(s.upper() if len(s) <= 3 else s.title() for s in ranked[:4])
