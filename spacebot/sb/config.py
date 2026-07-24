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
        "route": "llama3.1",
        "compose": "llama3.1",
        "vision": "llama3.2-vision",
    },
    "mock": {"route": "mock", "compose": "mock", "vision": "mock"},
}

# Routing / confidence thresholds (see pipeline.py).
ROUTE_MIN_SCORE = 0.40      # below this a candidate is discarded
AMBIGUOUS_MARGIN = 0.15     # top1-top2 gap under this => offer disambiguation
CONF_HIGH = 0.72
CONF_LOW = 0.45             # below this => abstain rather than risk a wrong answer


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
    "llm_provider": os.environ.get("SPACEBOT_PROVIDER", "auto"),  # auto|anthropic|openai|mock
    "anthropic_api_key": os.environ.get("ANTHROPIC_API_KEY", ""),
    "openai_api_key": os.environ.get("OPENAI_API_KEY", ""),
    "openai_base_url": os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
    "ollama_base_url": os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
    "route_model": os.environ.get("SPACEBOT_ROUTE_MODEL", ""),
    "compose_model": os.environ.get("SPACEBOT_COMPOSE_MODEL", ""),
    "vision_model": os.environ.get("SPACEBOT_VISION_MODEL", ""),
}
