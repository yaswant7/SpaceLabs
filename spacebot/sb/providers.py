"""Pluggable model providers — the reason Spacebot isn't married to one vendor.

Every provider implements the same four methods (route / compose / describe_image /
structure) and returns the same normalised dicts. The super-admin picks the provider and
supplies their own API key via /admin; the pipeline never knows which one it's talking to.

Adding Ollama / Azure / Bedrock later = one more subclass. No pipeline changes.
Implemented with the stdlib only (urllib) so the POC needs nothing installed.
"""
import base64
import json
import re
import urllib.error
import urllib.request
from collections import Counter

from . import config, prompts, settings


class ProviderError(RuntimeError):
    pass


def _extract_json(text: str) -> dict:
    text = (text or "").strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    # strip code fences / prose around the JSON object
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except Exception:
            pass
    raise ProviderError("model did not return valid JSON:\n" + text[:400])


def _post(url: str, headers: dict, payload: dict, timeout: int = None) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout or config.HTTP_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:500]
        raise ProviderError(f"HTTP {e.code} from {url}: {body}")
    except urllib.error.URLError as e:
        raise ProviderError(f"network error calling {url}: {e.reason}")


# ---------------------------------------------------------------------------
class Provider:
    name = "base"

    def route(self, question, cards):
        raise NotImplementedError

    def compose(self, question, packages, routing_note="", profile="", route_scores=None, spanning=False):
        raise NotImplementedError

    def describe_image(self, image_bytes, mime="image/png"):
        raise NotImplementedError

    def structure(self, material_text):
        raise NotImplementedError

    def health(self):
        return {"ok": True, "provider": self.name}

    def stream_markdown(self, question, packages, routing_note="", profile="", style="", spanning=False):
        """Default: compute the structured answer, then yield its markdown word-by-word.
        Real streaming providers override this to yield live model tokens."""
        from . import render
        ans = self.compose(question, packages, routing_note=routing_note, profile=profile,
                           spanning=spanning, style=style)
        for tok in re.findall(r"\S+\s*", render.answer_to_markdown(ans)):
            yield tok


# ---- Anthropic ------------------------------------------------------------
class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(self, s):
        self.key = s["anthropic_api_key"]
        self.route_model = s["route_model"]
        self.compose_model = s["compose_model"]
        self.vision_model = s["vision_model"]

    def _chat(self, system, user, model, images=None, max_tokens=2000):
        content = []
        for mime, b64 in (images or []):
            content.append({"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}})
        content.append({"type": "text", "text": user})
        payload = {
            "model": model, "max_tokens": max_tokens, "system": system,
            "messages": [{"role": "user", "content": content}],
        }
        headers = {
            "x-api-key": self.key,
            "anthropic-version": config.ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        resp = _post("https://api.anthropic.com/v1/messages", headers, payload)
        parts = [b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text"]
        return "".join(parts)

    def route(self, question, cards):
        user = prompts.ROUTE_USER.format(catalog=json.dumps(_cards_for_prompt(cards), indent=2), question=question)
        return _extract_json(self._chat(prompts.ROUTE_SYSTEM, user, self.route_model, max_tokens=800))

    def compose(self, question, packages, routing_note="", profile="", route_scores=None,
                spanning=False, style=""):
        user = prompts.COMPOSE_USER.format(
            packages=json.dumps(packages, indent=2), profile=profile or "new team member",
            question=question, routing_note=routing_note, style=style or "Clear and simple.")
        return _extract_json(self._chat(prompts.COMPOSE_SYSTEM, user, self.compose_model, max_tokens=2500))

    def describe_image(self, image_bytes, mime="image/png"):
        b64 = base64.b64encode(image_bytes).decode()
        return _extract_json(self._chat(prompts.VISION_SYSTEM, "Describe this screenshot.",
                                        self.vision_model, images=[(mime, b64)], max_tokens=700))

    def structure(self, material_text):
        return _extract_json(self._chat(prompts.STRUCTURE_SYSTEM, material_text[:12000],
                                        self.compose_model, max_tokens=2500))


# ---- OpenAI (and OpenAI-compatible, incl. local servers via base_url) ------
class OpenAIProvider(Provider):
    name = "openai"

    def __init__(self, s):
        self.key = s["openai_api_key"]
        self.base = s.get("openai_base_url", "https://api.openai.com/v1").rstrip("/")
        self.route_model = s["route_model"]
        self.compose_model = s["compose_model"]
        self.vision_model = s["vision_model"]
        self.timeout = config.HTTP_TIMEOUT

    def _chat(self, system, user, model, images=None, max_tokens=2000):
        if images:
            content = [{"type": "text", "text": user}]
            for mime, b64 in images:
                content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
            user_msg = {"role": "user", "content": content}
        else:
            user_msg = {"role": "user", "content": user}
        payload = {
            "model": model, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system}, user_msg],
            "response_format": {"type": "json_object"},
        }
        headers = {"Authorization": f"Bearer {self.key}", "content-type": "application/json"}
        resp = _post(f"{self.base}/chat/completions", headers, payload, self.timeout)
        return resp["choices"][0]["message"]["content"]

    def _chat_stream(self, system, user, model, max_tokens=1200):
        """Yield content tokens live from a streaming chat completion (SSE)."""
        payload = {"model": model, "max_tokens": max_tokens, "stream": True,
                   "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        req = urllib.request.Request(
            f"{self.base}/chat/completions", data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.key}", "content-type": "application/json"}, method="POST")
        try:
            resp = urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            raise ProviderError(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:200]}")
        except urllib.error.URLError as e:
            raise ProviderError(f"network error: {e.reason}")
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except Exception:
                continue
            delta = (obj.get("choices") or [{}])[0].get("delta", {}).get("content")
            if delta:
                yield delta

    def stream_markdown(self, question, packages, routing_note="", profile="", style="", spanning=False):
        user = prompts.COMPOSE_STREAM_USER.format(
            packages=json.dumps(packages, indent=2)[:12000], profile=profile or "new team member",
            question=question, style=style or "Clear and simple.")
        yield from self._chat_stream(prompts.COMPOSE_STREAM_SYSTEM, user, self.compose_model)

    def route(self, question, cards):
        user = prompts.ROUTE_USER.format(catalog=json.dumps(_cards_for_prompt(cards), indent=2), question=question)
        return _extract_json(self._chat(prompts.ROUTE_SYSTEM, user, self.route_model, max_tokens=800))

    def compose(self, question, packages, routing_note="", profile="", route_scores=None,
                spanning=False, style=""):
        user = prompts.COMPOSE_USER.format(
            packages=json.dumps(packages, indent=2), profile=profile or "new team member",
            question=question, routing_note=routing_note, style=style or "Clear and simple.")
        return _extract_json(self._chat(prompts.COMPOSE_SYSTEM, user, self.compose_model, max_tokens=2500))

    def describe_image(self, image_bytes, mime="image/png"):
        b64 = base64.b64encode(image_bytes).decode()
        return _extract_json(self._chat(prompts.VISION_SYSTEM, "Describe this screenshot.",
                                        self.vision_model, images=[(mime, b64)], max_tokens=700))

    def structure(self, material_text):
        return _extract_json(self._chat(prompts.STRUCTURE_SYSTEM, material_text[:12000],
                                        self.compose_model, max_tokens=2500))


# ---- Mock (no key, fully offline heuristic — keeps the POC runnable) -------
_WORD = re.compile(r"[a-z0-9_]+")
_JOURNEY = {"first", "week", "onboard", "onboarding", "start", "started", "getting",
            "new", "everything", "before", "setup", "set"}


def _toks(s):
    return set(_WORD.findall((s or "").lower()))


class MockProvider(Provider):
    name = "mock"

    def route(self, question, cards):
        q = _toks(question)
        ql = question.lower()
        scored = []
        for c in cards:
            keytoks = _toks(c["wf_key"].replace(".", " ").replace("_", " "))
            nametoks = _toks(c["name"])
            hay = _toks(c["summary"] + " " + c["category"] + " " +
                        " ".join(c.get("trigger_phrases", []))) | nametoks | keytoks
            overlap = len(q & hay)
            score = (overlap / max(len(q), 1)) * 0.8
            for tp in c.get("trigger_phrases", []):        # exact trigger phrase in the question
                if tp and tp.lower() in ql:
                    score += 0.4
            for t in q:                                    # a distinctive word hitting the name/id
                if len(t) >= 5 and (t in keytoks or t in nametoks):
                    score += 0.35
            score = min(score, 0.98)
            if score > 0.08:
                scored.append({"wf_key": c["wf_key"], "score": round(score, 3),
                               "why": f"{overlap} overlapping terms"})
        scored.sort(key=lambda x: x["score"], reverse=True)
        spanning = bool(q & _JOURNEY) and len([s for s in scored if s["score"] > 0.15]) >= 2
        return {"candidates": scored[:3], "spanning": spanning, "clarify": None,
                "out_of_scope": len(scored) == 0}

    def compose(self, question, packages, routing_note="", profile="", route_scores=None,
                spanning=False, style=""):
        route_scores = route_scores or {}
        if not packages:
            return {"abstain": True, "headline": "No matching workflow.", "confidence": 0.2,
                    "blocks": [], "sources": [], "alternatives": [], "followups": []}

        # journey across several workflows
        if spanning and len(packages) > 1:
            steps = []
            for p in packages:
                first = p["steps"][0]["body"] if p["steps"] else p["summary"]
                steps.append({"cite": f"{p['wf_key']}:step-1", "title": p["name"],
                              "body": p["summary"] or first, "verification": ""})
            return {
                "abstain": False,
                "headline": f"Here's the path — {len(packages)} workflows in order.",
                "primary_wf_key": packages[0]["wf_key"], "confidence": 0.7,
                "blocks": [{"type": "steps", "steps": steps}],
                "sources": [{"label": p["name"], "ref": p["wf_key"]} for p in packages],
                "alternatives": [], "followups": [f"How do I do {p['name'].lower()}?" for p in packages[:2]],
            }

        p = packages[0]
        blocks, sources = [], []
        # known-error shortcut
        ql = question.lower()
        for e in p.get("known_errors", []):
            if e["code"] and e["code"].lower() in ql:
                blocks.append({"type": "known_error", "code": e["code"],
                               "resolution": e["resolution"], "cite": f"{p['wf_key']}:error"})
                sources.append({"label": f"Known error {e['code']}", "ref": f"{p['wf_key']}:error"})
                break
        # the steps
        steps = [{"cite": f"{p['wf_key']}:{s['key']}", "title": s["title"], "body": s["body"],
                  "verification": s["verification"]} for s in p["steps"]]
        if steps:
            blocks.append({"type": "steps", "steps": steps})
            sources += [{"label": f"{p['name']} {s['key']}", "ref": f"{p['wf_key']}:{s['key']}"} for s in p["steps"]]
        conf = route_scores.get(p["wf_key"], 0.6)
        return {
            "abstain": not blocks,
            "headline": f"Here's how to {p['name'].lower()}." if blocks else "I don't have steps for this yet.",
            "primary_wf_key": p["wf_key"], "confidence": round(min(0.5 + conf / 2, 0.95), 2),
            "blocks": blocks, "sources": sources,
            "alternatives": [{"wf_key": x["wf_key"], "name": x["name"]} for x in packages[1:]],
            "followups": [f["question"] for f in p.get("faqs", [])[:2]],
        }

    def describe_image(self, image_bytes, mime="image/png"):
        return {"screen": "(mock mode — no vision)", "action": "", "text": "",
                "contains_secret": False, "alt_text": "image (mock)"}

    def structure(self, material_text):
        # strip the [modality anchor] provenance tags the pipeline prepends
        clean = re.sub(r"^\[[^\]]*\]\s*", "", material_text, flags=re.MULTILINE)
        paras = [x.strip() for x in clean.split("\n\n") if x.strip()]
        steps = [{"title": p.split("\n")[0][:60], "body": p, "verification": "", "tips": [], "mistakes": []}
                 for p in paras[:8]]
        # crude keyword extraction so the workflow is at least routable offline
        toks = [t for t in _WORD.findall(clean.lower()) if len(t) >= 5]
        triggers = [w for w, _ in Counter(toks).most_common(8)]
        return {"name": "Untitled workflow", "summary": paras[0][:220] if paras else "",
                "trigger_phrases": triggers, "steps": steps, "known_errors": [], "faqs": [],
                "uncertain": ["mock mode built this by paragraph — add a real model for good structure"]}


# ---- Ollama (local, OpenAI-compatible) -------------------------------------
class OllamaProvider(OpenAIProvider):
    """Local model via Ollama's OpenAI-compatible API. Same request shape as OpenAI,
    but no key needed and a longer timeout (local generation is slower). Every LLM step
    degrades gracefully to the heuristic Mock if Ollama is unreachable or returns bad JSON,
    so the app never crashes when the local server is down — it just gets simpler."""
    name = "ollama"

    def __init__(self, s):
        self.key = s.get("ollama_api_key") or "ollama"     # Ollama ignores auth
        self.base = (s.get("ollama_base_url") or "http://localhost:11434/v1").rstrip("/")
        self.route_model = s["route_model"]
        self.compose_model = s["compose_model"]
        self.vision_model = s["vision_model"]
        self.timeout = 300
        self._fb = MockProvider()

    def route(self, question, cards):
        try:
            return super().route(question, cards)
        except ProviderError:
            return self._fb.route(question, cards)

    def compose(self, question, packages, **kw):
        try:
            return super().compose(question, packages, **kw)
        except ProviderError:
            return self._fb.compose(question, packages, **kw)

    def structure(self, material_text):
        try:
            return super().structure(material_text)
        except ProviderError:
            return self._fb.structure(material_text)

    def stream_markdown(self, question, packages, **kw):
        gen = super().stream_markdown(question, packages, **kw)
        try:
            first = next(gen)
        except StopIteration:
            yield from self._fb.stream_markdown(question, packages, **kw)
            return
        except ProviderError:
            yield from self._fb.stream_markdown(question, packages, **kw)
            return
        yield first
        try:
            for tok in gen:
                yield tok
        except ProviderError:
            return   # keep whatever partial answer already streamed

    def health(self):
        try:
            req = urllib.request.Request(self.base + "/models",
                                         headers={"Authorization": f"Bearer {self.key}"})
            with urllib.request.urlopen(req, timeout=6) as r:
                data = json.loads(r.read().decode("utf-8"))
            models = [m.get("id") for m in (data.get("data") or [])]
            base = self.compose_model.split(":")[0]
            return {"ok": True, "reachable": True, "base": self.base, "models": models,
                    "compose_model": self.compose_model,
                    "model_ready": any(base in (m or "") for m in models)}
        except Exception as e:
            return {"ok": False, "reachable": False, "base": self.base, "error": str(e),
                    "hint": "Start Ollama (`ollama serve`) and pull the model, then check the base URL."}


def _cards_for_prompt(cards):
    return [{"wf_key": c["wf_key"], "name": c["name"], "summary": c["summary"],
             "category": c["category"], "trigger_phrases": c.get("trigger_phrases", [])} for c in cards]


def get_provider() -> Provider:
    s = settings.effective()
    p = s["resolved_provider"]
    if p == "anthropic":
        return AnthropicProvider(s)
    if p == "ollama":
        return OllamaProvider(s)
    if p == "openai":
        return OpenAIProvider(s)
    return MockProvider()
