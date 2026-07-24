"""Auth — local password hashing + session cookies (stdlib only).

Two roles matter for the product:
  - 'user'   : end user. Sees ONLY the chat. Never sees the workflow catalog.
  - 'author' : knowledge inputter. Chat + Studio (add knowledge, catalog, gaps).
  - 'admin'  : author + model settings.
"""
import binascii
import hashlib
import os

from . import db

COOKIE = "sb_sid"
_ITER = 120_000


def hash_pw(pw: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, _ITER)
    return binascii.hexlify(salt).decode() + "$" + binascii.hexlify(dk).decode()


def verify_pw(pw: str, stored: str) -> bool:
    try:
        salt_hex, hash_hex = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), binascii.unhexlify(salt_hex), _ITER)
        return binascii.hexlify(dk).decode() == hash_hex
    except Exception:
        return False


def authenticate(email: str, pw: str):
    u = db.get_user_by_email(email or "")
    if u and verify_pw(pw or "", u["pw"]):
        return u
    return None


def ensure_user(email: str, name: str, role: str, pw: str) -> str:
    """Idempotent user creation for seeding."""
    existing = db.get_user_by_email(email)
    if existing:
        return existing["id"]
    return db.create_user(email, name, role, hash_pw(pw))


def can_author(user) -> bool:
    return bool(user) and user.get("role") in ("author", "admin")


def is_admin(user) -> bool:
    return bool(user) and user.get("role") == "admin"
