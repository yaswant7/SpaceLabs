"""Central configuration.

Resolution order for any setting: DB `settings` table  >  environment  >  built-in default.
The DB layer is what the /admin page writes to, so a super-admin can point Spacebot
at their own model provider + API key at runtime without touching env or code.
"""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))     # .../spacebot/sb
ROOT_DIR = os.path.dirname(BASE_DIR)                       # .../spacebot
DATA_DIR = os.path.join(ROOT_DIR, "data")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
DB_PATH = os.environ.get("SPACEBOT_DB", os.path.join(DATA_DIR, "spacebot.db"))

ANTHROPIC_VERSION = "2023-06-01"
HTTP_TIMEOUT = 60
OLLAMA_TIMEOUT = 300        # local generation on CPU is slow; be patient rather than flaky

# Per-provider default model choices (cheap model for routing, strong for composing).
PROVIDER_MODELS = {
    "anthropic": {
        "route": "claude-haiku-4-5-20251001",
        "compose": "claude-sonnet-5",
        "vision": "claude-sonnet-5",
    },
    "openai": {
        "route": "gpt-4o-mini",
        "compose": "gpt-4o",
        "vision": "gpt-4o",
    },
    "ollama": {
        "route": "llama3.2:3b",
        "compose": "llama3.2:3b",
        "vision": "llama3.2-vision",
    },
    "mock": {"route": "mock", "compose": "mock", "vision": "mock"},
}

# Models we know work well here, best first. Used to pick a sensible default when the
# configured Ollama model isn't pulled but something else is.
OLLAMA_PREFERENCE = ["llama3.2:3b", "llama3.1:8b", "qwen2.5:7b", "qwen2.5:3b",
                     "mistral:7b", "phi3.5", "gemma2:9b"]

# Generation knobs for local models. num_ctx has to be big enough to hold a whole
# workflow package plus the conversation.
OLLAMA_OPTIONS = {"temperature": 0.3, "top_p": 0.9, "num_ctx": 8192}

# Routing, condensing and structuring are classification, not writing. Sampling them adds
# nothing and costs reproducibility — the same question would route to a workflow one
# minute and abstain the next. Greedy decoding makes the demo repeatable.
OLLAMA_DETERMINISTIC = {"temperature": 0, "top_p": 1, "num_ctx": 8192, "seed": 7}

# Routing / confidence thresholds (see pipeline.py).
#
# ROUTE_MIN_SCORE sits at 0.50, not 0.40, and the gap matters. The router prompt defines
# 0.4–0.5 as "adjacent — plausibly contains part of the answer", and answering out of an
# adjacent workflow is exactly how you get a confidently wrong answer. Measured on this
# corpus: genuine matches score 0.6–0.9, paraphrases ("what should I do on day one?")
# score 0.8, and the one observed false positive scored precisely 0.40. Abstaining on a
# 0.4 is the behaviour we want.
ROUTE_MIN_SCORE = 0.50      # below this a candidate is discarded
# Bar for the BM25 fallback to rescue a question the router dropped. Higher than the
# router floor on purpose: a lexical hit with no semantic vote behind it must be clearly
# strong before we act on it.
LEXICAL_MIN_SCORE = 0.55
AMBIGUOUS_MARGIN = 0.15     # top1-top2 gap under this => offer disambiguation
CONF_HIGH = 0.72
CONF_LOW = 0.45             # below this => abstain rather than risk a wrong answer

# Conversation memory: how many prior turns the composer sees, and how many are used
# to rewrite a follow-up ("what about staging?") into a standalone routable question.
HISTORY_TURNS = 6
CONDENSE_TURNS = 4

# ---- RAG: chunking, embeddings, hybrid retrieval --------------------------
#
# Chunk size is a retrieval decision, not a storage one. ~1200 characters with a 180
# overlap keeps a procedure step or a CV section whole in one chunk (so an answer isn't
# split across two) while staying small enough that a match is precise rather than "this
# document mentions it somewhere".
CHUNK_CHARS = 1200
CHUNK_OVERLAP = 180
CHUNK_MIN = 120             # shorter than this gets merged into its neighbour

EMBED_MODEL = "nomic-embed-text"    # Apache-2.0, 768-dim, ~270MB, CPU-friendly
EMBED_BATCH = 48
EMBED_TIMEOUT = 180

# Hybrid retrieval. Dense catches paraphrase, BM25 catches rare literals (names, error
# codes, commands) that embeddings blur. Reciprocal-rank fusion merges them without
# needing the two score scales to be comparable — which they are not.
RETRIEVE_CANDIDATES = 40    # per retriever, before fusion
RETRIEVE_CHUNKS = 10        # chunks handed to the composer after fusion
RRF_K = 60                  # standard RRF damping constant
# Keep only chunks scoring within this fraction of the best hit. A fixed top-K always
# fills its quota, padding a focused question with unrelated documents — which is how a
# CV and an on-call rota ended up in one context and the model reported the person in the
# CV as being on call.
RELATIVE_CUTOFF = 0.45
MAX_WORKFLOWS_IN_ANSWER = 4 # how many distinct workflows one answer may combine

# How much a chunk gains for being ABOUT the person or system the question names (see
# sb/subjects.py). Applied to the fused score before the cut, so subject-matching chunks
# can climb into the answer rather than merely reorder inside it.
#
# 0.35 is large enough to lift the right person's excerpts over a same-shaped document that
# happens to score well — a CV and an on-call rota look alike to every retriever we run —
# and small enough that it cannot manufacture relevance from nothing: a chunk with no
# lexical or semantic support has a fused score near zero, and a third of nothing is still
# nothing. It is a boost and never a filter, so a mis-extracted subject can only reorder,
# never suppress.
SUBJECT_BOOST = 0.35

# How much of a question one single document must cover before we answer from the set.
#
# This catches the failure every other guard misses: a question whose words all appear in
# the corpus, but scattered across documents that have nothing to do with it. Asked "how to
# read github secrets of a project I've access", retrieval scored 0.549 — `github` from a
# CV, `access` from the AWS runbook, `project` from the CV again — and answered with the VPN
# and AWS steps. Nothing was about GitHub secrets. Every retriever agreed, which is exactly
# why the evidence score rose.
#
# Measured on this corpus, coverage by the best single document separates cleanly:
#
#     answerable       0.33 – 1.00
#     not answerable   0.00 – 0.25
#
# 0.30 sits in that gap. The two answerable questions at the 0.33 floor are the known
# tokeniser artefacts ("on-call" indexed whole, "expenses" never unpluralised), so the real
# margin for ordinary questions is wider than it looks.
MIN_QUERY_COVERAGE = 0.30

# Evidence gates for the answering policy (see pipeline._policy). Measured, not guessed —
# see retrieval._evidence for the probe results these come from. On this corpus every
# answerable question scored >= 0.53 and every unanswerable one <= 0.08, so there is a
# wide margin; these sit inside it rather than on either edge.
EVIDENCE_STRONG = 0.40      # answer it
EVIDENCE_WEAK = 0.15        # below this we genuinely have nothing — say so, warmly


def _load_dotenv():
    path = os.path.join(ROOT_DIR, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()
os.makedirs(UPLOAD_DIR, exist_ok=True)


# Env-level defaults (DB overrides these; see settings.effective()).
ENV_DEFAULTS = {
    # Whose assistant is this? Every system prompt introduces itself with these, so a fresh
    # clone must not claim to work for whoever the code was written for. Blank is a valid
    # answer — the prompts fall back to neutral wording ("your team") rather than a name.
    "org_name": os.environ.get("SPACEBOT_ORG_NAME", ""),
    "assistant_name": os.environ.get("SPACEBOT_ASSISTANT_NAME", "Spacebot"),
    "llm_provider": os.environ.get("SPACEBOT_PROVIDER", "auto"),  # auto|anthropic|openai|mock
    "anthropic_api_key": os.environ.get("ANTHROPIC_API_KEY", ""),
    "openai_api_key": os.environ.get("OPENAI_API_KEY", ""),
    "openai_base_url": os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
    "ollama_base_url": os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434"),
    "route_model": os.environ.get("SPACEBOT_ROUTE_MODEL", ""),
    "compose_model": os.environ.get("SPACEBOT_COMPOSE_MODEL", ""),
    "vision_model": os.environ.get("SPACEBOT_VISION_MODEL", ""),
}
