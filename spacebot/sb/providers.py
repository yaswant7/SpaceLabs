"""Pluggable model providers — the reason Spacebot isn't married to one vendor.

Every provider implements the same small surface (route / compose / stream_markdown /
condense / describe_image / structure) and returns the same normalised dicts. The
super-admin picks the provider and supplies their own key via /admin; the pipeline never
knows which one it's talking to.

Ollama is the default and speaks Ollama's native API rather than the OpenAI shim: it gives
us `format: json` (real constrained decoding, which small local models badly need),
NDJSON streaming, and per-request options like num_ctx. Adding Azure / Bedrock later is
one more subclass — no pipeline changes. Stdlib only (urllib), so nothing to install.
"""
import base64
import json
import re
import urllib.error
import urllib.request
from collections import Counter

from . import config, prompts, render, settings


class ProviderError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# JSON coercion. Small local models are enthusiastic but sloppy: they wrap objects in
# prose, fence them, or emit trailing commentary. We recover rather than fail.
# ---------------------------------------------------------------------------
_FENCE = re.compile(r"```(?:json)?\s*(.+?)```", re.S)


def _balanced_object(text: str):
    """First balanced {...} in the text, respecting strings and escapes."""
    start = text.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
        start = text.find("{", start + 1)
    return None


def _extract_json(text: str) -> dict:
    text = (text or "").strip()
    if not text:
        raise ProviderError("model returned an empty response")
    candidates = [text]
    m = _FENCE.search(text)
    if m:
        candidates.append(m.group(1).strip())
    bal = _balanced_object(text)
    if bal:
        candidates.append(bal)
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except Exception:
            continue
        if isinstance(obj, dict):
            return obj
        if isinstance(obj, list) and obj and isinstance(obj[0], dict):
            return obj[0]
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
    except TimeoutError:
        raise ProviderError(f"timed out calling {url}")


def _history_messages(history, limit=None):
    """Normalise stored turns into role/content messages the model can consume."""
    limit = config.HISTORY_TURNS * 2 if limit is None else limit
    out = []
    for m in (history or [])[-limit:]:
        role = "assistant" if m.get("role") in ("assistant", "bot") else "user"
        text = (m.get("content") or "").strip()
        if text:
            out.append({"role": role, "content": text[:4000]})
    return out


def _history_text(history, limit=None):
    msgs = _history_messages(history, limit)
    if not msgs:
        return "(this is the first message in the conversation)"
    label = {"user": "Team member", "assistant": "Spacebot"}
    return "\n".join(f"{label[m['role']]}: {m['content']}" for m in msgs)


# ---------------------------------------------------------------------------
class Provider:
    name = "base"
    degraded_reason = ""        # set when a provider silently fell back; surfaced in the UI

    def route(self, question, cards):
        raise NotImplementedError

    def compose(self, question, packages, routing_note="", profile="", route_scores=None,
                spanning=False, style="", history=None):
        raise NotImplementedError

    def condense(self, question, history):
        """Rewrite a follow-up into a standalone question for routing.
        Default: no history means nothing to condense."""
        return question

    def title(self, question, answer=""):
        """Name a conversation from its first exchange. "" means "caller should fall back".

        Implemented once here rather than per provider: every provider that can chat can do
        this, and the only argument that differs between their `_chat` signatures is the
        token cap — which this doesn't need, because the reply is a few words of JSON and
        stops on its own.

        Never raises. A title is a nicety; failing to produce one must not cost the user
        their answer, so every failure path returns "" and lets the caller keep the
        question-derived title it already showed them.
        """
        chat = getattr(self, "_chat", None)
        if not chat or not (question or "").strip():
            return ""
        user = prompts.TITLE_USER.format(question=(question or "")[:500],
                                         answer=(answer or "")[:700])
        try:
            out = _extract_json(chat(prompts.TITLE_SYSTEM, user, self.route_model))
        except Exception:
            return ""
        title = (out or {}).get("title")
        if not isinstance(title, str):
            return ""
        # Models wrap titles in quotes and end them with a full stop however plainly you ask
        # them not to, and both look wrong in a sidebar.
        title = " ".join(title.split()).strip(" \"'“”.·-—")

        # Measured: the model reaches for "… Not Found" whenever the answer was that we hold
        # nothing, despite being told not to. It is wrong as a label — it describes today's
        # corpus, not the topic, and stops being true the moment somebody documents it — and
        # it costs a third of the width the sidebar has. Trim it rather than lose the title.
        for tail in (" not found", " not available", " unavailable", " not documented",
                     " not on file", " information", " details"):
            if title.lower().endswith(tail):
                title = title[: -len(tail)].rstrip(" :–—-")
                break

        # The cap is generous on purpose. Both titles truncate at this width, so the
        # question is which one reads better cut off — and a model title leads with the
        # subject ("Rolling back a production deploy…") where the raw question leads with
        # "how do I…". Rejecting a 43-character title in favour of a 38-character question
        # traded a good label for a worse one. Only genuinely runaway output falls back.
        return title if 2 <= len(title) <= 52 else ""

    def describe_image(self, image_bytes, mime="image/png"):
        raise NotImplementedError

    def structure(self, material_text):
        raise NotImplementedError

    def health(self):
        return {"ok": True, "provider": self.name}

    def stream_rag(self, question, context, policy, profile="", history=None, style=""):
        """Stream an answer from retrieved excerpts. This is the primary answering path.

        Providers that can't stream fall back to yielding the whole thing at once, which
        the UI handles identically."""
        raise NotImplementedError

    def stream_markdown(self, question, packages, routing_note="", profile="", style="",
                        spanning=False, history=None, hint=""):
        """Default: compute the structured answer, then yield its markdown word-by-word.
        Real streaming providers override this to yield live model tokens."""
        ans = self.compose(question, packages, routing_note=routing_note, profile=profile,
                           spanning=spanning, style=style, history=history)
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
        system = prompts.render(system)
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

    def condense(self, question, history):
        if not history:
            return question
        user = prompts.CONDENSE_USER.format(history=_history_text(history, config.CONDENSE_TURNS * 2),
                                            question=question)
        try:
            out = _extract_json(self._chat(prompts.CONDENSE_SYSTEM, user, self.route_model, max_tokens=200))
            return (out.get("standalone_question") or question).strip() or question
        except ProviderError:
            return question

    def compose(self, question, packages, routing_note="", profile="", route_scores=None,
                spanning=False, style="", history=None):
        user = prompts.COMPOSE_USER.format(
            packages=render.packages_for_prompt(packages), profile=profile or "new team member",
            question=question, routing_note=routing_note, style=style or "Clear and simple.",
            history=_history_text(history))
        return _extract_json(self._chat(prompts.COMPOSE_SYSTEM, user, self.compose_model, max_tokens=2500))

    def describe_image(self, image_bytes, mime="image/png"):
        b64 = base64.b64encode(image_bytes).decode()
        return _extract_json(self._chat(prompts.VISION_SYSTEM, "Describe this screenshot.",
                                        self.vision_model, images=[(mime, b64)], max_tokens=700))

    def structure(self, material_text):
        return _extract_json(self._chat(prompts.STRUCTURE_SYSTEM, material_text[:12000],
                                        self.compose_model, max_tokens=2500))


# ---- OpenAI (and OpenAI-compatible, incl. Azure / vLLM via base_url) -------
class OpenAIProvider(Provider):
    name = "openai"

    def __init__(self, s):
        self.key = s["openai_api_key"]
        self.base = s.get("openai_base_url", "https://api.openai.com/v1").rstrip("/")
        self.route_model = s["route_model"]
        self.compose_model = s["compose_model"]
        self.vision_model = s["vision_model"]
        self.timeout = config.HTTP_TIMEOUT

    def _messages(self, system, user, images=None, history=None):
        msgs = [{"role": "system", "content": system}] + _history_messages(history)
        if images:
            content = [{"type": "text", "text": user}]
            for mime, b64 in images:
                content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
            msgs.append({"role": "user", "content": content})
        else:
            msgs.append({"role": "user", "content": user})
        return msgs

    def _chat(self, system, user, model, images=None, max_tokens=2000, json_mode=True, history=None):
        system = prompts.render(system)
        payload = {"model": model, "max_tokens": max_tokens,
                   "messages": self._messages(system, user, images, history)}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self.key}", "content-type": "application/json"}
        resp = _post(f"{self.base}/chat/completions", headers, payload, self.timeout)
        return resp["choices"][0]["message"]["content"]

    def _chat_stream(self, system, user, model, max_tokens=1400, history=None):
        """Yield content tokens live from a streaming chat completion (SSE)."""
        system = prompts.render(system)
        payload = {"model": model, "max_tokens": max_tokens, "stream": True,
                   "messages": self._messages(system, user, history=history)}
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

    def stream_markdown(self, question, packages, routing_note="", profile="", style="",
                        spanning=False, history=None, hint=""):
        user = prompts.COMPOSE_STREAM_USER.format(
            packages=render.packages_for_prompt(packages), profile=profile or "new team member",
            question=question, style=style or "Clear and simple.", hint=hint)
        yield from self._chat_stream(prompts.COMPOSE_STREAM_SYSTEM, user, self.compose_model,
                                     history=history)

    def route(self, question, cards):
        user = prompts.ROUTE_USER.format(catalog=json.dumps(_cards_for_prompt(cards), indent=2), question=question)
        return _extract_json(self._chat(prompts.ROUTE_SYSTEM, user, self.route_model, max_tokens=800))

    def condense(self, question, history):
        if not history:
            return question
        user = prompts.CONDENSE_USER.format(history=_history_text(history, config.CONDENSE_TURNS * 2),
                                            question=question)
        try:
            out = _extract_json(self._chat(prompts.CONDENSE_SYSTEM, user, self.route_model, max_tokens=200))
            return (out.get("standalone_question") or question).strip() or question
        except ProviderError:
            return question

    def compose(self, question, packages, routing_note="", profile="", route_scores=None,
                spanning=False, style="", history=None):
        user = prompts.COMPOSE_USER.format(
            packages=render.packages_for_prompt(packages), profile=profile or "new team member",
            question=question, routing_note=routing_note, style=style or "Clear and simple.",
            history=_history_text(history))
        return _extract_json(self._chat(prompts.COMPOSE_SYSTEM, user, self.compose_model, max_tokens=2500))

    def describe_image(self, image_bytes, mime="image/png"):
        b64 = base64.b64encode(image_bytes).decode()
        return _extract_json(self._chat(prompts.VISION_SYSTEM, "Describe this screenshot.",
                                        self.vision_model, images=[(mime, b64)], max_tokens=700))

    def structure(self, material_text):
        return _extract_json(self._chat(prompts.STRUCTURE_SYSTEM, material_text[:12000],
                                        self.compose_model, max_tokens=2500))


# ---- Ollama (local, native API) -------------------------------------------
class OllamaProvider(Provider):
    """Local model over Ollama's own API.

    Native rather than the OpenAI shim because we want `format: "json"` — grammar-constrained
    decoding, which is the difference between a 3B model reliably returning our AnswerDocument
    and returning an apology. Every step degrades to the heuristic MockProvider if Ollama is
    unreachable or the output is unusable, so a demo never dies on a cold server; when that
    happens we record `degraded_reason` so the UI can say so instead of pretending.
    """
    name = "ollama"

    def __init__(self, s):
        base = (s.get("ollama_base_url") or "http://localhost:11434").rstrip("/")
        if base.endswith("/v1"):
            base = base[:-3]
        self.base = base
        self.route_model = s["route_model"]
        self.compose_model = s["compose_model"]
        self.vision_model = s["vision_model"]
        self.timeout = config.OLLAMA_TIMEOUT
        self._fb = MockProvider()

    # -- transport ----------------------------------------------------------
    def _messages(self, system, user, images=None, history=None):
        msgs = [{"role": "system", "content": system}] + _history_messages(history)
        um = {"role": "user", "content": user}
        if images:
            um["images"] = [b64 for _mime, b64 in images]
        msgs.append(um)
        return msgs

    def _chat(self, system, user, model, images=None, json_mode=True, num_predict=2048,
              history=None, deterministic=False):
        system = prompts.render(system)
        base = config.OLLAMA_DETERMINISTIC if deterministic else config.OLLAMA_OPTIONS
        payload = {
            "model": model,
            "messages": self._messages(system, user, images, history),
            "stream": False,
            "options": {**base, "num_predict": num_predict},
        }
        if json_mode:
            payload["format"] = "json"
        resp = _post(f"{self.base}/api/chat", {"content-type": "application/json"},
                     payload, self.timeout)
        return (resp.get("message") or {}).get("content", "")

    def _chat_stream(self, system, user, model, num_predict=1600, history=None):
        """Ollama streams NDJSON: one JSON object per line, each with a content delta."""
        system = prompts.render(system)
        payload = {
            "model": model,
            "messages": self._messages(system, user, history=history),
            "stream": True,
            "options": {**config.OLLAMA_OPTIONS, "num_predict": num_predict},
        }
        req = urllib.request.Request(
            f"{self.base}/api/chat", data=json.dumps(payload).encode("utf-8"),
            headers={"content-type": "application/json"}, method="POST")
        try:
            resp = urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as e:
            raise ProviderError(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:200]}")
        except (urllib.error.URLError, OSError) as e:
            raise ProviderError(f"cannot reach Ollama at {self.base}: {e}")
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if obj.get("error"):
                raise ProviderError(str(obj["error"]))
            piece = (obj.get("message") or {}).get("content", "")
            if piece:
                yield piece
            if obj.get("done"):
                break

    def _degrade(self, step, err):
        self.degraded_reason = f"{step} fell back to the offline heuristic ({err})"

    # -- capabilities -------------------------------------------------------
    def route(self, question, cards):
        user = prompts.ROUTE_USER.format(catalog=json.dumps(_cards_for_prompt(cards), indent=2),
                                         question=question)
        try:
            return _extract_json(self._chat(prompts.ROUTE_SYSTEM, user, self.route_model,
                                            num_predict=700, deterministic=True))
        except ProviderError as e:
            self._degrade("routing", e)
            return self._fb.route(question, cards)

    def condense(self, question, history):
        if not history:
            return question
        user = prompts.CONDENSE_USER.format(history=_history_text(history, config.CONDENSE_TURNS * 2),
                                            question=question)
        try:
            out = _extract_json(self._chat(prompts.CONDENSE_SYSTEM, user, self.route_model,
                                           num_predict=180, deterministic=True))
            rewritten = (out.get("standalone_question") or "").strip()
            # A rewrite that loses the question entirely is worse than no rewrite.
            return rewritten if 3 < len(rewritten) < 400 else question
        except ProviderError:
            return question

    def compose(self, question, packages, routing_note="", profile="", route_scores=None,
                spanning=False, style="", history=None):
        user = prompts.COMPOSE_USER.format(
            packages=render.packages_for_prompt(packages), profile=profile or "new team member",
            question=question, routing_note=routing_note, style=style or "Clear and simple.",
            history=_history_text(history))
        try:
            return _extract_json(self._chat(prompts.COMPOSE_SYSTEM, user, self.compose_model,
                                            num_predict=2048))
        except ProviderError as e:
            self._degrade("composing", e)
            return self._fb.compose(question, packages, routing_note=routing_note, profile=profile,
                                    route_scores=route_scores, spanning=spanning, style=style)

    def structure(self, material_text):
        try:
            return _extract_json(self._chat(prompts.STRUCTURE_SYSTEM, material_text[:12000],
                                            self.compose_model, num_predict=2048,
                                            deterministic=True))
        except ProviderError as e:
            self._degrade("structuring", e)
            return self._fb.structure(material_text)

    def describe_image(self, image_bytes, mime="image/png"):
        b64 = base64.b64encode(image_bytes).decode()
        try:
            return _extract_json(self._chat(prompts.VISION_SYSTEM, "Describe this screenshot.",
                                            self.vision_model, images=[(mime, b64)], num_predict=600))
        except ProviderError as e:
            self._degrade("image understanding", e)
            return self._fb.describe_image(image_bytes, mime)

    def stream_rag(self, question, context, policy, profile="", history=None, style=""):
        user = prompts.RAG_USER.format(
            context=context or "(nothing was retrieved)", history=_history_text(history),
            profile=profile or "A team member", question=question,
            policy=(policy + ("\n" + style if style else "")))
        gen = self._chat_stream(prompts.RAG_SYSTEM, user, self.compose_model,
                                history=None, num_predict=1400)
        try:
            first = next(gen)
        except StopIteration:
            self._degrade("answering", "empty response")
            return
        except ProviderError as e:
            self._degrade("answering", e)
            raise
        yield first
        try:
            for tok in gen:
                yield tok
        except (ProviderError, OSError):
            return

    def stream_markdown(self, question, packages, routing_note="", profile="", style="",
                        spanning=False, history=None, hint=""):
        user = prompts.COMPOSE_STREAM_USER.format(
            packages=render.packages_for_prompt(packages), profile=profile or "new team member",
            question=question, style=style or "Clear and simple.", hint=hint)
        gen = self._chat_stream(prompts.COMPOSE_STREAM_SYSTEM, user, self.compose_model,
                                history=history)
        # Pull the first token before committing: if the server is down we can still fall
        # back cleanly. After the first token we're committed — a mid-stream failure keeps
        # whatever already reached the user rather than replacing it with different text.
        try:
            first = next(gen)
        except StopIteration:
            self._degrade("streaming", "empty response")
            yield from self._fb.stream_markdown(question, packages, routing_note=routing_note,
                                                profile=profile, style=style, spanning=spanning)
            return
        except ProviderError as e:
            self._degrade("streaming", e)
            yield from self._fb.stream_markdown(question, packages, routing_note=routing_note,
                                                profile=profile, style=style, spanning=spanning)
            return
        yield first
        try:
            for tok in gen:
                yield tok
        except (ProviderError, OSError):
            return

    def health(self):
        try:
            with urllib.request.urlopen(self.base + "/api/tags", timeout=6) as r:
                data = json.loads(r.read().decode("utf-8"))
            models = [m.get("name") for m in (data.get("models") or [])]
            return {"ok": True, "reachable": True, "provider": self.name, "base": self.base,
                    "models": models, "compose_model": self.compose_model,
                    "model_ready": self.compose_model in models}
        except Exception as e:
            return {"ok": False, "reachable": False, "provider": self.name, "base": self.base,
                    "error": str(e),
                    "hint": "Start Ollama (`ollama serve`), pull a model "
                            "(`ollama pull llama3.2:3b`), then re-test."}


# ---- Mock (no model, fully offline heuristic — keeps the POC runnable) -----
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

    def condense(self, question, history):
        """No model: stitch the last user turn on when the follow-up is too short to route
        on its own ("what about staging?"). Crude, but better than routing on 3 words."""
        if not history or len(_WORD.findall(question.lower())) > 6:
            return question
        prev = next((m["content"] for m in reversed(history) if m.get("role") == "user"), "")
        return f"{prev} {question}".strip() if prev else question

    def compose(self, question, packages, routing_note="", profile="", route_scores=None,
                spanning=False, style="", history=None):
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

    def stream_rag(self, question, context, policy, profile="", history=None, style=""):
        """Extractive fallback when no model is reachable.

        Retrieval is pure Python and keeps working without a model, so with no LLM we can
        still show the user the passages that answer their question — just unpolished
        rather than written. That keeps the app usable on a machine with Ollama stopped,
        which is the whole point of having an offline mode.
        """
        if not context or context.startswith("(nothing"):
            yield ("I don't have anything on that yet. If you can tell me a bit more about "
                   "what you're trying to do, I'll point you at the closest thing we have.")
            return
        yield "Here's what we have on file:\n\n"
        for line in context.splitlines():
            line = line.strip()
            if not line or line.startswith("###"):
                continue
            body = re.sub(r"^\[[^\]]+\]\s*", "", line)
            if body:
                yield f"- {body}\n"

    def describe_image(self, image_bytes, mime="image/png"):
        return {"screen": "(offline mode — no vision model)", "action": "", "text": "",
                "contains_secret": False, "alt_text": "image (offline mode)"}

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
                "uncertain": ["offline mode built this by paragraph — connect a model for good structure"]}


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
