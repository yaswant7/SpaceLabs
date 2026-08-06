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


# ---- OCR -------------------------------------------------------------------
class Ocr:
    """Text inside an image or a scanned page.

    Tried before the vision model because it is ~100x cheaper and exact on printed text —
    a vision model paraphrases what it reads, which is fine for "what is this a screenshot
    of" and wrong for "what does this invoice say".
    """
    def available(self) -> bool:
        if shutil.which("tesseract") is None:
            return False
        try:
            import pytesseract  # noqa: F401
            return True
        except ImportError:
            return False

    def read(self, image_bytes: bytes) -> str:
        if not self.available():
            raise CapabilityUnavailable(
                "OCR needs tesseract and pytesseract — install the binary "
                "(apt-get install tesseract-ocr) then: pip install --user pytesseract")
        import io
        import pytesseract
        from PIL import Image
        try:
            return (pytesseract.image_to_string(Image.open(io.BytesIO(image_bytes))) or "").strip()
        except Exception as e:
            raise CapabilityUnavailable(f"OCR failed: {e}")


def get_ocr() -> Ocr:
    return Ocr()


# ---- Image understanding ----------------------------------------------------
class ImageUnderstander:
    """OCR first for literal text, then the vision model for what the image shows.

    Both are optional and each covers a different need, so we take whatever is available
    and record honestly when neither is.
    """
    def describe(self, image_bytes: bytes, mime="image/png") -> dict:
        ocr_text = ""
        ocr = get_ocr()
        if ocr.available():
            try:
                ocr_text = ocr.read(image_bytes)
            except CapabilityUnavailable:
                ocr_text = ""

        try:
            desc = providers.get_provider().describe_image(image_bytes, mime)
        except Exception as e:
            if not ocr_text:
                raise CapabilityUnavailable(f"no OCR and no vision model available ({e})")
            desc = {"screen": "", "action": "", "text": ocr_text, "alt_text": ""}

        parts = [desc.get("screen"), desc.get("action"), desc.get("text")]
        if ocr_text and ocr_text not in (desc.get("text") or ""):
            parts.append("Text in image: " + ocr_text)
        text = " ".join(x for x in parts if x).strip()
        desc["_text"] = text
        desc["_ocr"] = bool(ocr_text)
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
