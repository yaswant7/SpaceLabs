"""Embeddings — local, open-source, swappable.

Runs `nomic-embed-text` through Ollama: 768 dimensions, Apache-2.0, ~270MB, and fast
enough on CPU that embedding a document is not the slow part of ingestion. No API key,
no network, nothing leaves the machine.

The Embedder interface is the seam. Swapping to bge-m3, e5, or a hosted endpoint is one
subclass; swapping the *store* underneath (SQLite → pgvector → Qdrant) is `sb/vectors.py`.
Neither touches the pipeline.

Vectors are stored as raw float32 bytes rather than JSON — a third of the size, and it
decodes into a numpy array with no parsing.
"""
import json
import struct
import urllib.error
import urllib.request

from . import config, settings

try:
    import numpy as np
except ImportError:                     # pragma: no cover - numpy is a hard dep in practice
    np = None


class EmbeddingUnavailable(RuntimeError):
    """Raised when no embedding model is reachable. Callers fall back to lexical search
    rather than failing the request — a degraded answer beats no answer."""


def pack(vec) -> bytes:
    """float32 little-endian, the same layout numpy.frombuffer reads back."""
    if np is not None:
        return np.asarray(vec, dtype="<f4").tobytes()
    return struct.pack(f"<{len(vec)}f", *vec)


def unpack(blob: bytes):
    if np is not None:
        return np.frombuffer(blob, dtype="<f4")
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


class Embedder:
    name = "base"
    dim = 0

    def embed(self, texts: list) -> list:
        raise NotImplementedError

    def embed_one(self, text: str):
        return self.embed([text])[0]

    def health(self) -> dict:
        return {"ok": False, "model": self.name}


class OllamaEmbedder(Embedder):
    """Ollama's /api/embed. Batches natively, so ingestion embeds a whole document in one
    round trip rather than one call per chunk."""
    dim = 768

    def __init__(self, base: str, model: str = None):
        self.base = (base or "http://localhost:11434").rstrip("/")
        if self.base.endswith("/v1"):
            self.base = self.base[:-3]
        self.name = model or config.EMBED_MODEL

    def embed(self, texts: list) -> list:
        texts = [t if (t or "").strip() else " " for t in texts]
        out = []
        # Chunked so one enormous document can't build a multi-megabyte request body.
        for i in range(0, len(texts), config.EMBED_BATCH):
            batch = texts[i:i + config.EMBED_BATCH]
            payload = {"model": self.name, "input": batch}
            req = urllib.request.Request(
                f"{self.base}/api/embed", data=json.dumps(payload).encode("utf-8"),
                headers={"content-type": "application/json"}, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=config.EMBED_TIMEOUT) as r:
                    data = json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace")[:200]
                raise EmbeddingUnavailable(f"HTTP {e.code} from Ollama embed: {body}")
            except (urllib.error.URLError, OSError, TimeoutError) as e:
                raise EmbeddingUnavailable(f"cannot reach Ollama at {self.base}: {e}")
            vecs = data.get("embeddings") or ([data["embedding"]] if data.get("embedding") else [])
            if len(vecs) != len(batch):
                raise EmbeddingUnavailable(
                    f"expected {len(batch)} embeddings, got {len(vecs)}")
            out.extend(vecs)
        if out:
            self.dim = len(out[0])
        return out

    def health(self) -> dict:
        try:
            v = self.embed(["health check"])[0]
            return {"ok": True, "model": self.name, "dim": len(v), "base": self.base}
        except EmbeddingUnavailable as e:
            return {"ok": False, "model": self.name, "base": self.base, "error": str(e),
                    "hint": f"run: ollama pull {self.name}"}


_cache = {"key": None, "embedder": None}


def get_embedder() -> Embedder:
    """Cached per (base, model) so we don't rebuild on every request."""
    s = settings.effective()
    base = s.get("ollama_base_url", "")
    model = s.get("embed_model") or config.EMBED_MODEL
    key = (base, model)
    if _cache["key"] != key:
        _cache.update({"key": key, "embedder": OllamaEmbedder(base, model)})
    return _cache["embedder"]


def cosine_matrix(query_vec, matrix):
    """Cosine similarity of one query against a stacked matrix of candidates.

    Rows are L2-normalised at write time, so this is a single dot product — one BLAS call
    for the whole corpus, which is what keeps brute-force search viable well past the
    point where a naive Python loop would fall over.
    """
    if np is None:
        return [sum(a * b for a, b in zip(query_vec, row)) for row in matrix]
    return matrix @ query_vec


def normalize(vec):
    if np is None:
        n = sum(v * v for v in vec) ** 0.5 or 1.0
        return [v / n for v in vec]
    arr = np.asarray(vec, dtype="<f4")
    n = float(np.linalg.norm(arr)) or 1.0
    return arr / n
