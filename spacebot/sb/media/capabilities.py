"""Capability providers — the heavy, external, swappable media operations.

Each capability is an interface with (a) a local implementation for dev and (b) a clear
seam for a cloud or in-house implementation the customer plugs in. None of the pipeline
code changes when you switch providers — exactly like the LLM provider seam.

  Transcriber     audio/video -> transcript with word timestamps + speakers
  FrameExtractor  video -> scene boundaries + keyframes
  ImageUnderstander  image -> {screen, action, text, contains_secret}  (uses the LLM vision provider)

If a capability isn't configured/available, we raise CapabilityUnavailable and the job
records "awaiting capability" rather than failing or guessing. That is the honest behaviour.
"""
import re
import shutil

from .. import providers, settings

SECRET_PATTERNS = [
    re.compile(r"\b(sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16})\b"),
    re.compile(r"(?i)\b(password|passwd|secret|api[_-]?key|token)\b\s*[:=]\s*\S+"),
    re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),   # emails / possible PII
]


class CapabilityUnavailable(RuntimeError):
    pass


def scan_secrets(text: str) -> bool:
    return any(p.search(text or "") for p in SECRET_PATTERNS)


# ---- Image understanding (available now via the LLM vision provider) -------
class ImageUnderstander:
    def describe(self, image_bytes: bytes, mime="image/png") -> dict:
        # Uses whichever model provider is configured (Claude/OpenAI vision, or mock).
        desc = providers.get_provider().describe_image(image_bytes, mime)
        text = " ".join(x for x in [desc.get("screen"), desc.get("action"), desc.get("text")] if x).strip()
        desc["_text"] = text
        desc["contains_secret"] = bool(desc.get("contains_secret") or scan_secrets(text))
        return desc


# ---- Transcription ---------------------------------------------------------
class Transcriber:
    def transcribe(self, blob_path: str, mime: str) -> dict: ...


class LocalWhisper(Transcriber):
    """Uses a local whisper CLI if present. Absent on most machines -> unavailable."""
    def available(self):
        return shutil.which("whisper") is not None or shutil.which("whisper-ctranslate2") is not None

    def transcribe(self, blob_path, mime):
        if not self.available():
            raise CapabilityUnavailable("no local whisper binary — set a cloud transcriber in settings")
        # Real impl would shell out and parse word timestamps; kept out of the POC.
        raise CapabilityUnavailable("local whisper wiring not enabled in POC")


class CloudTranscriber(Transcriber):
    """Deepgram / AssemblyAI / customer endpoint. Reads key from settings."""
    def __init__(self, s):
        self.key = s.get("transcriber_api_key", "")
        self.endpoint = s.get("transcriber_endpoint", "")

    def transcribe(self, blob_path, mime):
        if not self.key:
            raise CapabilityUnavailable("cloud transcriber selected but no transcriber_api_key set")
        raise CapabilityUnavailable("cloud transcriber endpoint not wired in POC — plug your provider here")


# ---- Frame extraction ------------------------------------------------------
class FrameExtractor:
    def scenes(self, blob_path: str) -> list: ...


class LocalFFmpeg(FrameExtractor):
    def available(self):
        return shutil.which("ffmpeg") is not None

    def scenes(self, blob_path):
        if not self.available():
            raise CapabilityUnavailable("no ffmpeg — install it or use a cloud frame extractor")
        raise CapabilityUnavailable("local ffmpeg scene wiring not enabled in POC")


# ---- Registry (picks impl from settings; 'auto' prefers local if available) ----
def get_transcriber() -> Transcriber:
    s = settings.effective()
    choice = (s.get("transcriber") or "auto").lower()
    if choice in ("cloud", "deepgram", "assemblyai"):
        return CloudTranscriber(s)
    lw = LocalWhisper()
    if choice == "local" or (choice == "auto" and lw.available()):
        return lw
    return CloudTranscriber(s)   # auto with no local -> cloud (which will ask for a key)


def get_frame_extractor() -> FrameExtractor:
    return LocalFFmpeg()


def get_image_understander() -> ImageUnderstander:
    return ImageUnderstander()
