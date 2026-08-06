"""Who or what a chunk is about.

A chunk with no idea whose knowledge it carries is how a knowledge base answers "any info
about Sreedhar Masula?" with Yaswanth's CV. Retrieval found text that was *near* the
question — a document about a person, when a person was asked about — and nothing
downstream knew that the person in the document was somebody else. The model did what
models do with a plausible context: it bridged.

So every chunk records its subjects: the named people, companies and systems the text is
about. That one piece of metadata does three jobs.

  * RANKING    a question naming someone lifts the excerpts that are about them, so the
               right person's document reaches the top of the model's context.
  * ABSTAINING a question naming a subject nothing is about is answered as "we have
               nothing on that", which is the truth, instead of from whatever ranked.
  * LABELLING  each excerpt reaches the model tagged with its subject, so even when
               several subjects legitimately share one answer the boundary is visible.

Note what is NOT on that list: filtering. Subjects influence order and they can stop an
answer entirely, but they never quietly delete evidence from a context that still gets
answered. Extraction is a heuristic, and a heuristic allowed to drop chunks turns a wrong
answer into a missing one — which is worse, because nobody reports it.

Extraction is statistical, not a named-entity model: no extra dependency, no service to
run, and it works on the names an enterprise actually has, which are exactly the ones a
pretrained NER model has never seen. Three signals, each earned by a failure:

  * CAPITALISATION, measured across the whole corpus. A token is a name if the corpus
    writes it capitalised far more than lowercase. Measured per-document this fails —
    every document capitalises its own topic words in headings and rarely writes them
    lowercase, so "Policy", "Week" and "Status" all looked like names.
  * POSITION. Sentence-initial capitals carry no information, and runbook prose is
    imperative, so counting them made "Roll", "Revert", "Confirm" and "Get" the names of
    the people this company documents.
  * A COMMON-WORD LIST, for single tokens only. Statistics can tell that a token is used
    as a name *in this corpus*; they cannot tell that "create" is a verb in the language,
    because a corpus that only ever writes it in step titles offers no counter-evidence.
    Multi-word runs are exempt, so names built from ordinary words — "Modern Signal",
    "Acme Ltd" — survive.

Precision matters more than recall throughout. A subject we miss leaves today's behaviour
in place; a subject we invent misdirects a good question. So every rule under-claims:
headings are excluded, single mentions are ignored, and a name shared by most of the
corpus is dropped from gating as undiscriminating.
"""
import re
from collections import Counter

_WORDS = re.compile(r"\b[A-Za-z][A-Za-z'’]*(?:[-][A-Za-z'’]+)*")

# Ordinary English. A capitalised word that is also a common word tells us nothing — every
# corpus capitalises "Create", "Status", "Week" and "Policy" in a title or a step heading,
# and treating those as the names of things the company documents is how "how do I create a
# local dev environment" ends up gated onto the purchase-order workflow.
#
# This is the one place a word list is the right tool. The capitalisation statistics can
# tell that a token is being used as a name *in this corpus*; they cannot tell that "create"
# is a verb in the language, because a corpus that only ever writes it in step titles offers
# no counter-evidence. Names built FROM ordinary words ("Modern Signal", "Acme Ltd") are not
# lost — they are recognised as multi-word runs, which this filter never sees.
_COMMON = frozenset("""
about above accept access account across act action active add address advance after again
against age agree ahead all allow almost alone along already also although always among
amount and animal another answer any anyone anything appear apply approach approve area
argue arm around arrive art article ask assume attack attempt attention author available
avoid away back bad bag balance ball bank base based basic be bear beat beautiful because
become bed been before begin behind being believe below benefit best better between beyond
big bill bit black block blood blue board boat body book born both bottom box boy break
bring broad brother budget build building business but buy by call can capital car card
care career carry case cash catch cause cell center central century certain chain chair
challenge chance change channel character charge check child choice choose city civil claim
class clean clear client close code cold collect college color come comfort command comment
commit common community company compare complete computer concern condition conference
confirm connect consider contain content continue contract control cook copy core corner
cost could count country couple course court cover create crime cross culture current
customer cut damage dance danger dark data date daughter day deal death debate decade decide
decision deep defense degree deliver demand democratic department depend describe design
desk despite detail determine develop device die difference different difficult dinner
direct direction director discover discuss discussion disease do doctor document dog door
double down draw dream drive drop drug during duty each early earn earth ease east easy eat
economic economy edge education effect effort eight either election else employee end energy
engine enjoy enough enter entire environment equal error especially establish even evening
event ever every evidence exactly example execute exist expect experience expert explain
export express extend extra eye face fact factor fail fall family far fast father fear
feature federal feel feeling few field fight figure file fill film final finally financial
find fine finger finish fire firm first fish fit five fix floor flow focus follow food foot
for force foreign forget form former forward found four free friend from front full fund
future gain game gap garden gas general generate get girl give glass global go goal good
government great green ground group grow growth guess guide gun guy hair half hand handle
hang happen happy hard have he head health hear heart heat heavy help her here herself high
him himself his history hit hold home hope hospital host hot hotel hour house housing how
however huge human hundred husband idea identify if image imagine impact important improve
in include increase indeed index indicate individual industry information inside instead
institution insurance interest internal international interview into introduce invest
involve issue it item its itself job join just keep key kid kill kind kitchen knife know
knowledge labor lack land language large last late later laugh law lawyer lay lead leader
learn least leave left legal length less let letter level lie life light like likely limit
line link list listen little live load local location long look lose loss lot love low
machine magazine main maintain major make man manage management manager many map mark market
marriage material matter may maybe me mean measure media medical meet meeting member memory
mention message method middle might military million mind minute miss mission model modern
moment money month more morning most mother motion mount move movement movie much music must
my myself name nation national natural nature near nearly necessary need network never new
news next nice night nine no none nor north not note nothing notice now number nurse object
observe obtain occur of off offer office officer official often oil old on once one only
open operate operation opportunity option or order organization original other others our
out output outside over overall owner page pain paper parent park part particular partner
party pass past patient pattern pay peace people per perform performance perhaps period
person personal phone physical pick picture piece place plan plant play player please point
police policy political politics poor popular population position positive possible post
power practice prepare present president press pressure pretty prevent previous price
primary print private probably problem process produce product production professional
program project property propose protect prove provide public pull purchase purpose push put
quality question quickly quite race radio raise range rapid rate rather reach read ready
real reality realize really reason receive recent recognize record reduce refer reflect
region relate relationship release remain remember remove repeat replace report represent
request require research resource respond response responsibility rest result return reveal
review rich right rise risk road rock role room rule run safe safety sale same save say
scale scene school science score screen sea search season seat second section sector secure
security see seek seem sell send senior sense series serious serve service session set seven
several sex shake share she shoot short shot should shoulder show side sign significant
similar simple simply since sing single sister sit site situation six size skill skin small
smile so social society soft software soldier solution some somebody someone something
sometimes son song soon sort sound source south space speak special specific speed spend
sport spring staff stage stand standard star start state statement station status stay step
still stock stop store story strategy street strong structure student study stuff style
subject success such suddenly suffer suggest summer supply support sure surface system table
take talk task tax teach teacher team technology telephone television tell ten tend term
test text than thank that the their them themselves then theory there these they thing think
third this those though thought thousand threat three through throughout throw thus time to
today together tone tonight too top total tough toward town trade traditional traffic train
transfer travel treat treatment tree trial trip trouble true trust truth try turn two type
under understand unit until up upon us use user usually value various very victim view visit
voice vote wait walk wall want war watch water way we wear week weight welcome well west
what whatever when where whether which while white who whole whom whose why wide wife will
win wind window wish with within without woman word work worker world worry would write
writer wrong yard yeah year yes yet you young your yourself
""".split())

# Words capitalised for structural reasons that a common-word list wouldn't catch, plus the
# markup our own chunk format introduces.
_NEVER = _COMMON | {
    "faq", "faqs", "heads", "tip", "tips", "cv", "resume", "qa", "todo", "eg", "ie",
}

_MIN_LEN = 3
_MIN_MENTIONS = 2          # a name mentioned once is a passing reference, not a subject
_CAP_RATIO = 2.0           # capitalised at least twice as often as lowercase
_UBIQUITY = 0.5            # a "subject" in over half the corpus discriminates nothing

_SENT_SPLIT = re.compile(r"(?<=[.!?:;])\s+|\s*[|•]\s*|\s+[—–-]\s+")


def _norm(word: str) -> str:
    """Lowered, with the possessive removed.

    Worth spelling out because getting it wrong silently disabled the whole mechanism:
    people write "Meena's vendors" and "yaswanth's role", so without stripping `'s` the
    corpus records a subject called "meena's" that no question ever matches, and the two
    names this feature exists to tell apart are both invisible.
    """
    w = word.lower().rstrip("'’")
    for suf in ("'s", "’s"):
        if w.endswith(suf):
            w = w[: -len(suf)]
    return w.strip("'’")


def _is_common(word: str) -> bool:
    """Is this ordinary English? Plural-aware, because the word list is singular.

    Without the plural check "skills", "covers" and "vendors" all read as names purely
    because the list happens to hold "skill", "cover" and "vendor".
    """
    w = _norm(word)
    if w in _NEVER:
        return True
    for suf in ("es", "s"):
        if len(w) > len(suf) + 2 and w.endswith(suf) and w[: -len(suf)] in _NEVER:
            return True
    return w.endswith("ing") and len(w) > 5 and w[:-3] in _NEVER


def _prose_lines(text: str) -> list:
    """Body lines, minus headings.

    Chunk text carries a heading on its first line ("Expense Policy — step 2: …") and
    spreadsheet chunks are rows of Title Case cells. Both would teach a capitalisation
    test that "Policy" and "Week" are names. Dropping lines that are almost entirely
    capitalised removes the class at its source instead of blocklisting its output.
    """
    out = []
    for line in (text or "").splitlines():
        s = line.strip()
        if not s:
            continue
        words = _WORDS.findall(s)
        if not words:
            continue
        caps = sum(1 for w in words if w[0].isupper())
        if len(words) <= 8 and caps >= max(2, len(words) - 1):
            continue
        out.append(s)
    return out


def _counts(texts) -> tuple:
    """(capitalised, lowercase) counts per token, ignoring sentence-initial position.

    The position rule is what makes this work at all. Runbook prose is imperative — "Revert
    the release.", "Confirm the pods are healthy.", "Roll back to the last good revision."
    — so the first word of nearly every sentence is a capitalised verb. Counting those, the
    extractor decided `roll`, `revert`, `confirm`, `get` and `submit` were the names of the
    people and systems this company documents.

    A word's capitalisation only carries information away from the start of a sentence, so
    that is the only place it is counted. Real names survive easily: they recur mid-sentence
    ("…projects Yaswanth built", "…raise it in SAP"), which is precisely what an imperative
    verb never does.
    """
    upper, lower, multi = Counter(), Counter(), Counter()
    _JOINERS = {"of", "and", "for", "the", "de", "van", "der", "&"}

    for t in texts:
        for line in _prose_lines(t):
            for sent in _SENT_SPLIT.split(line):
                words = _WORDS.findall(sent)
                caps = [bool(w[0].isupper()) for w in words]

                # Maximal runs of adjacent capitalised words. Two or more capitals in a row
                # is a name even when every word in it is ordinary English — "Modern Signal",
                # "Acme Ltd", "Payment Gateway Integration" — so runs are counted separately
                # and are exempt from the common-word filter that single tokens must pass.
                i = 0
                while i < len(words):
                    if not caps[i]:
                        i += 1
                        continue
                    # A run may not begin on an ordinary word. Runbook prose is imperative,
                    # so "Open Argo CD" and "Get IAM access" put a capitalised verb in front
                    # of a real name; counting the verb as part of the name made `open`,
                    # `get`, `install` and `request` subjects of half the corpus.
                    if _is_common(words[i]):
                        i += 1
                        continue
                    j = i + 1
                    while j < len(words) and (caps[j] or
                                              (words[j].lower() in _JOINERS
                                               and j + 1 < len(words) and caps[j + 1])):
                        j += 1
                    if j - i >= 2:
                        for w in words[i:j]:
                            key = _norm(w)
                            if len(key) >= _MIN_LEN and key not in _JOINERS:
                                multi[key] += 1
                    i = j

                for k, w in enumerate(words):
                    key = _norm(w)
                    if len(key) < _MIN_LEN or _is_common(w):
                        continue
                    if k == 0:
                        # Position, not the word, explains this capital — except when the
                        # word wears a possessive. "Meena's approved vendor list" opens a
                        # sentence, and the `'s` is the only evidence in the document that
                        # Meena is a person. Ignoring it lost the subject entirely.
                        if w.lower().endswith(("'s", "’s")):
                            upper[key] += 1
                        continue
                    (upper if w[0].isupper() else lower)[key] += 1
    return upper, lower, multi


def names_in(texts) -> set:
    """The tokens this text treats as proper names.

    Two routes in. A token repeatedly appearing inside a multi-word capitalised run is a
    name regardless of what it means in English. A token standing alone must both survive
    the common-word filter and be capitalised well beyond its lowercase use here.
    """
    upper, lower, multi = _counts(texts)
    out = {tok for tok, n in multi.items() if n >= _MIN_MENTIONS}
    out |= {tok for tok, n in upper.items()
            if n >= _MIN_MENTIONS and n >= _CAP_RATIO * max(1, lower.get(tok, 0))}
    return out


def index(packages: list) -> dict:
    """Subjects for the whole corpus at once: {wf_key: {"workflow": set, "per_text": […]}}.

    Name-hood is decided ACROSS the corpus, not within one document, and that is the
    difference between a usable signal and a noisy one. Judged inside a single workflow,
    "Policy", "Week", "Create" and "Status" look like names — each document uses its own
    topic words in headings and rarely in lower case. Judged across every document, the
    same words turn up lowercase in someone else's prose ("the policy covers…", "create a
    purchase order") and are correctly rejected, while "Yaswanth", "Acme", "Tailscale" and
    "PagerDuty" never do.

    `packages` is [(pkg, [chunk_text, …]), …].
    """
    corpus = []
    for pkg, texts in packages:
        corpus += [pkg.get("name") or "", pkg.get("summary") or ""] + list(texts)
    global_names = names_in(corpus)

    out = {}
    for pkg, texts in packages:
        # Subjects named by the ingestion model. This is the portable signal — no tuning,
        # any language, and it can name a subject mentioned only once — so it is preferred
        # over the statistics below. It is not, however, taken at face value.
        #
        # Asked for proper names, a small model also returns topic phrases: alongside "Argo
        # CD" and "Yaswanth Kamineni" it offered "revert a broken staging release" and
        # "vendor approval". Accepting those reintroduces exactly the false positives the
        # statistics were built to avoid — `revert` and `approval` become the names of
        # things this company documents.
        #
        # Capitalisation in the model's own answer separates them cleanly. A name is
        # written as a name; a description of the topic is not. Lowercase tokens are
        # dropped, so a phrase like "vendor approval" contributes nothing while "Acme Ltd"
        # contributes both its words.
        declared = set()
        for s in pkg.get("subjects") or []:
            for w in _WORDS.findall(str(s)):
                key = _norm(w)
                if len(key) >= _MIN_LEN and key not in _NEVER and w[0].isupper():
                    declared.add(key)

        # Matched on whole words, not substrings. Substring matching attributed "NET" to
        # every document containing "network" or "internet", which both pollutes the label
        # a document carries and — because subject_miss is decided from these sets — could
        # make a question look answered by a document that never mentions its subject.
        per_words = [{_norm(w) for w in _WORDS.findall(t or "")} for t in texts]
        doc_words = {_norm(w) for w in
                     _WORDS.findall(f"{pkg.get('name') or ''} {pkg.get('summary') or ''}")}
        for ws in per_words:
            doc_words |= ws
        wf = declared | (global_names & doc_words)

        # A name is a subject of a chunk if the chunk actually mentions it; chunks that name
        # nobody inherit the document's subjects so nothing is left unattributed.
        per_text = [sorted((wf & ws) or wf) for ws in per_words]
        out[pkg["wf_key"]] = {"workflow": wf, "per_text": per_text}
    return out


def discriminating(by_workflow: dict) -> set:
    """Subjects worth GATING on — a stricter set than the ones worth labelling with.

    Labelling and gating want different things, and separating them is what keeps this
    safe. A label is free: tagging an excerpt `about: Modern Signal` costs nothing if
    "Signal" is a shaky extraction. A gate is not: acting on a bad subject suppresses
    correct evidence. So the gate takes only subjects that clear two extra bars.

      * NOT ORDINARY ENGLISH. A multi-word run legitimately makes "Modern" and "Payment"
        subjects for labelling, but a question containing the word "payment" must not be
        redirected at whichever document happens to own that run.
      * NOT UBIQUITOUS. A name shared by most of the corpus — "SpaceLabs" in a SpaceLabs
        knowledge base — is real but filters nothing.

    What survives is the set this feature exists for: people, vendors and named systems.
    """
    if not by_workflow:
        return set()
    df = Counter()
    for names in by_workflow.values():
        for n in names:
            df[n] += 1
    limit = max(1, int(len(by_workflow) * _UBIQUITY))
    return {n for n, c in df.items() if c <= limit and not _is_common(n)}


_FRAME = {"about", "regarding", "named", "called", "concerning", "re"}


def unknown_runs(query: str, is_known) -> list:
    """Names in the question that the knowledge base has never heard of.

    This is the other half of subject awareness. Gating on known subjects can only redirect
    a question toward the right document; it cannot notice that the person being asked about
    has no document at all. "Any info about Sreedhar Masula?" retrieved a CV, an on-call rota
    and a vendor list — all plausibly about *people* — and the model wrote Sreedhar a career
    out of Yaswanth's.

    `is_known(token)` reports whether a token appears anywhere in the corpus. Corpus absence
    alone is far too blunt to act on, and measuring proved it: `call`, `limit`, `expenses`
    and `32` are all absent from the index — artefacts of tokenising "on-call" whole and of
    "expenses" never appearing unpluralised — yet the questions containing them answer
    perfectly. So absence only counts when the word also looks like a name:

      * two or more adjacent unknown words — the shape of a first and last name, of a
        company, of a product. Tokeniser artefacts do not come in pairs.
      * one unknown word wearing a possessive ("sreedhar's role"), which is a name marker
        on its own.
      * one unknown word introduced as a name ("about Xyz", "a vendor called Xyz").

    Ordinary English words never qualify, whatever their corpus status, which is what keeps
    a paraphrased question from being mistaken for an unknown entity.
    """
    words = _WORDS.findall(query or "")
    unknown = []
    for w in words:
        key = _norm(w)
        unknown.append(len(key) >= _MIN_LEN and not _is_common(w) and not is_known(key))

    runs, i = [], 0
    while i < len(words):
        if not unknown[i]:
            i += 1
            continue
        j = i
        while j < len(words) and unknown[j]:
            j += 1
        span = words[i:j]
        possessive = any(w.lower().endswith(("'s", "’s")) for w in span)
        framed = i > 0 and _norm(words[i - 1]) in _FRAME
        if len(span) >= 2 or possessive or framed:
            runs.append(" ".join(_norm(w) for w in span))
        i = j
    return runs


def mentions(query: str, known: set) -> set:
    """Which known subjects the question names.

    Matched case-insensitively against the query's words, because people type "meena" and
    "yaswanth" in lower case and a gate that only fires on correct capitalisation is a gate
    that never fires.
    """
    q = {_norm(w) for w in _WORDS.findall(query or "")}
    return {n for n in known if n in q}
