"""Blob storage — content-addressed. Originals and derived artifacts (page renders,
video keyframes) live here, keyed by sha256 so identical uploads dedupe automatically.

LocalDiskBlob is the POC implementation. S3Blob / R2Blob / the customer's own bucket
implement the SAME interface — swapping is a config change, not a rewrite. Production adds
presigned direct-to-bucket uploads so large media never proxies through the API.
"""
import hashlib
import os

from .. import config


class BlobStore:
    def put(self, data: bytes, ext: str = "") -> str: ...
    def get(self, key: str) -> bytes: ...
    def path(self, key: str) -> str: ...
    def url(self, key: str) -> str: ...


class LocalDiskBlob(BlobStore):
    def __init__(self, root=None):
        self.root = root or os.path.join(config.DATA_DIR, "blobs")
        os.makedirs(self.root, exist_ok=True)

    def _p(self, key):
        return os.path.join(self.root, key)

    def put(self, data: bytes, ext: str = "") -> str:
        digest = hashlib.sha256(data).hexdigest()
        key = digest + (ext if ext.startswith(".") else (f".{ext}" if ext else ""))
        p = self._p(key)
        if not os.path.exists(p):
            with open(p, "wb") as fh:
                fh.write(data)
        return key

    def get(self, key: str) -> bytes:
        with open(self._p(key), "rb") as fh:
            return fh.read()

    def path(self, key: str) -> str:
        return self._p(key)

    def url(self, key: str) -> str:
        # POC: served via the app behind auth. Production: signed, short-lived bucket URL.
        return f"/blob/{key}"


_store = None


def store() -> BlobStore:
    global _store
    if _store is None:
        _store = LocalDiskBlob()          # swap to S3Blob(settings) here later
    return _store
