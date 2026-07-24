"""Source adapters — turn one raw upload into a list of Segment descriptors.

A descriptor is a plain dict the pipeline persists/enriches:
  {modality, text, image_bytes?, anchor, meta}

Add a modality = add an adapter here. Nothing downstream changes, because everything
downstream consumes Segments, not files. That is the whole point.
"""
import io

from .capabilities import CapabilityUnavailable, get_frame_extractor, get_transcriber


def guess_kind(mime: str, filename: str) -> str:
    m = (mime or "").lower()
    f = (filename or "").lower()
    if "pdf" in m or f.endswith(".pdf"):
        return "pdf"
    if m.startswith("image/") or f.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
        return "image"
    if m.startswith("video/") or f.endswith((".mp4", ".mov", ".webm", ".mkv")):
        return "video"
    if m.startswith("audio/") or f.endswith((".mp3", ".wav", ".m4a", ".ogg")):
        return "audio"
    return "transcript"


class Adapter:
    def decompose(self, data: bytes, filename: str) -> list:
        raise NotImplementedError


class PdfAdapter(Adapter):
    """PDF = a mix of text and embedded screenshots. Each page -> a text segment;
    each embedded image on the page -> its own image segment, anchored to that page."""
    def decompose(self, data, filename):
        try:
            import pypdf
        except ImportError:
            return [{"modality": "text", "text": "[PDF parsing needs pypdf]", "anchor": {}}]
        reader = pypdf.PdfReader(io.BytesIO(data))
        out = []
        for i, page in enumerate(reader.pages):
            txt = (page.extract_text() or "").strip()
            if txt:
                out.append({"modality": "pdf_page", "text": txt, "anchor": {"page": i + 1}})
            try:
                for j, img in enumerate(page.images):
                    out.append({"modality": "image", "image_bytes": img.data,
                                "anchor": {"page": i + 1, "image_index": j},
                                "meta": {"from_pdf": True, "name": getattr(img, "name", "")}})
            except Exception:
                pass   # some PDFs have unreadable image streams; text still captured
        return out or [{"modality": "text", "text": "[empty PDF]", "anchor": {}}]


class ImageAdapter(Adapter):
    """A single screenshot. A *sequence* of screenshots is just several ImageAdapter
    sources uploaded in order — the pipeline preserves global order across the batch,
    so a 6-screenshot sequence naturally becomes 6 ordered step candidates."""
    def decompose(self, data, filename):
        return [{"modality": "image", "image_bytes": data, "anchor": {}, "meta": {"filename": filename}}]


class TranscriptAdapter(Adapter):
    """Plain text / markdown / a pasted call transcript. Split into coherent chunks;
    keep speaker turns if present."""
    def decompose(self, data, filename):
        text = data.decode("utf-8", "replace") if isinstance(data, bytes) else str(data)
        chunks, buf = [], []
        for line in text.splitlines():
            if line.strip() == "":
                if buf:
                    chunks.append("\n".join(buf).strip())
                    buf = []
            else:
                buf.append(line)
        if buf:
            chunks.append("\n".join(buf).strip())
        return [{"modality": "text", "text": c, "anchor": {"para": i + 1}}
                for i, c in enumerate(chunks) if c] or \
               [{"modality": "text", "text": text.strip(), "anchor": {}}]


class VideoAdapter(Adapter):
    """Screen recording. Real flow: extract scenes+keyframes (FrameExtractor) and a
    time-stamped transcript (Transcriber), then align them into video_scene segments,
    each carrying its narration text + representative keyframe + [t_start,t_end].
    If those capabilities aren't configured, we raise so the job records 'awaiting
    capability' and keeps the original blob for when a provider is plugged in."""
    def decompose(self, data, filename):
        fx = get_frame_extractor()
        tx = get_transcriber()
        # both raise CapabilityUnavailable until a provider is wired — honest by design
        scenes = fx.scenes("<blob>")           # -> [{t_start,t_end,keyframe_bytes}]
        transcript = tx.transcribe("<blob>", "video/mp4")
        segs = []
        for k, sc in enumerate(scenes):
            narration = _narration_for(transcript, sc["t_start"], sc["t_end"])
            segs.append({"modality": "video_scene", "text": narration,
                         "image_bytes": sc.get("keyframe_bytes"),
                         "anchor": {"t_start": sc["t_start"], "t_end": sc["t_end"]},
                         "meta": {"scene": k}})
        return segs


class AudioAdapter(Adapter):
    def decompose(self, data, filename):
        tx = get_transcriber()
        transcript = tx.transcribe("<blob>", "audio/mp3")   # raises until configured
        return [{"modality": "audio", "text": seg["text"],
                 "anchor": {"t_start": seg["start"], "t_end": seg["end"]}}
                for seg in transcript.get("segments", [])]


def _narration_for(transcript, t0, t1):
    return " ".join(s["text"] for s in transcript.get("segments", [])
                    if s["start"] >= t0 and s["end"] <= t1)


_REGISTRY = {
    "pdf": PdfAdapter(), "image": ImageAdapter(), "transcript": TranscriptAdapter(),
    "video": VideoAdapter(), "audio": AudioAdapter(),
}


def pick_adapter(mime: str, filename: str) -> Adapter:
    return _REGISTRY[guess_kind(mime, filename)]
