"""Effective-settings resolution: DB settings override env defaults override built-ins.

This is what makes Spacebot model-agnostic. The /admin page writes provider + API key
into the DB `settings` table; everything downstream just calls effective() and never
hardcodes a vendor.

`auto` is deliberately local-first: if an Ollama server is answering, use it. Only then
fall back to a hosted key, and only then to the offline heuristic. That means a fresh
clone with Ollama running is a real LLM install with zero configuration.
"""
import json
import time
import urllib.error
import urllib.request

from . import config, db

_probe_cache = {"at": 0.0, "base": None, "models": None}
_PROBE_TTL = 10.0          # seconds — keeps `auto` cheap on every request


def ollama_models(base_url: str, timeout: float = 2.0, force: bool = False):
    """Tags installed on the Ollama server, or None if it isn't reachable.
    Cached briefly so resolving settings on every request stays free."""
    base = (base_url or "").rstrip("/")
    if base.endswith("/v1"):                 # tolerate an OpenAI-style URL in settings
        base = base[:-3]
    now = time.time()
    if (not force and _probe_cache["base"] == base
            and now - _probe_cache["at"] < _PROBE_TTL):
        return _probe_cache["models"]
    models = None
    try:
        with urllib.request.urlopen(base + "/api/tags", timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        models = [m.get("name", "") for m in (data.get("models") or [])]
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        models = None
    _probe_cache.update({"at": now, "base": base, "models": models})
    return models


def pick_ollama_model(installed, wanted: str = "") -> str:
    """Resolve the model to actually call.

    Exact match wins. Then a tag-less match (`llama3.2` matches `llama3.2:3b`), so a
    user typing a family name still works. Then our preference list. Then whatever is
    installed. Returning the wanted name unchanged when nothing is installed keeps the
    error message honest instead of silently swapping models.
    """
    installed = installed or []
    if wanted:
        if wanted in installed:
            return wanted
        stem = wanted.split(":")[0]
        for m in installed:
            if m.split(":")[0] == stem:
                return m
    for pref in config.OLLAMA_PREFERENCE:
        if pref in installed:
            return pref
        for m in installed:
            if m.split(":")[0] == pref.split(":")[0]:
                return m
    return installed[0] if installed else (wanted or config.PROVIDER_MODELS["ollama"]["compose"])


def normalize_ollama_url(url: str) -> str:
    """Accept whatever someone pastes — with or without the OpenAI-style /v1 suffix,
    with or without a trailing slash — and store one canonical native base URL."""
    base = (url or "").strip().rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    return base or "http://localhost:11434"


def effective() -> dict:
    s = dict(config.ENV_DEFAULTS)
    s.update({k: v for k, v in db.get_settings_dict().items() if v not in (None, "")})
    s["ollama_base_url"] = normalize_ollama_url(s.get("ollama_base_url"))

    provider = (s.get("llm_provider") or "auto").lower()
    installed = None
    if provider in ("auto", "ollama"):
        installed = ollama_models(s.get("ollama_base_url", ""))

    if provider == "auto":
        if installed:                              # a local model is running — prefer it
            provider = "ollama"
        elif s.get("anthropic_api_key"):
            provider = "anthropic"
        elif s.get("openai_api_key"):
            provider = "openai"
        else:
            provider = "mock"       # nothing available -> heuristic mode, still fully runnable
    s["resolved_provider"] = provider

    models = config.PROVIDER_MODELS.get(provider, config.PROVIDER_MODELS["mock"])
    s["route_model"] = s.get("route_model") or models["route"]
    s["compose_model"] = s.get("compose_model") or models["compose"]
    s["vision_model"] = s.get("vision_model") or models["vision"]

    if provider == "ollama":
        s["ollama_installed"] = installed or []
        s["ollama_reachable"] = installed is not None
        # Point at something that is actually pulled, so the demo never 404s on a model.
        s["route_model"] = pick_ollama_model(installed, s["route_model"])
        s["compose_model"] = pick_ollama_model(installed, s["compose_model"])
    return s


def public_view() -> dict:
    """Settings safe to render in the admin UI (keys masked)."""
    s = effective()

    def mask(v):
        return (v[:4] + "…" + v[-4:]) if v and len(v) > 8 else ("set" if v else "")

    return {
        "llm_provider": s.get("llm_provider", "auto"),
        "resolved_provider": s["resolved_provider"],
        "anthropic_api_key_masked": mask(s.get("anthropic_api_key", "")),
        "openai_api_key_masked": mask(s.get("openai_api_key", "")),
        "openai_base_url": s.get("openai_base_url", ""),
        "ollama_base_url": s.get("ollama_base_url", ""),
        "ollama_installed": s.get("ollama_installed", []),
        "ollama_reachable": s.get("ollama_reachable", False),
        "route_model": s["route_model"],
        "compose_model": s["compose_model"],
        "vision_model": s["vision_model"],
    }
