"""Effective-settings resolution: DB settings override env defaults override built-ins.

This is what makes Spacebot model-agnostic. The /admin page writes provider + API key
into the DB `settings` table; everything downstream just calls effective() and never
hardcodes a vendor.
"""
from . import config, db


def effective() -> dict:
    s = dict(config.ENV_DEFAULTS)
    s.update({k: v for k, v in db.get_settings_dict().items() if v not in (None, "")})

    provider = (s.get("llm_provider") or "auto").lower()
    if provider == "auto":
        if s.get("anthropic_api_key"):
            provider = "anthropic"
        elif s.get("openai_api_key"):
            provider = "openai"
        else:
            provider = "mock"       # no key anywhere -> heuristic mode, still fully runnable
    s["resolved_provider"] = provider

    models = config.PROVIDER_MODELS.get(provider, config.PROVIDER_MODELS["mock"])
    s["route_model"] = s.get("route_model") or models["route"]
    s["compose_model"] = s.get("compose_model") or models["compose"]
    s["vision_model"] = s.get("vision_model") or models["vision"]
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
        "route_model": s["route_model"],
        "compose_model": s["compose_model"],
        "vision_model": s["vision_model"],
    }
