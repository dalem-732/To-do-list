import hashlib
import hmac
import os
import secrets
from base64 import urlsafe_b64decode, urlsafe_b64encode

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_PREFIX = "enc1:"


def _decode_data_key(raw: str) -> bytes:
    try:
        key = bytes.fromhex(raw)
    except ValueError:
        try:
            key = urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        except ValueError as exc:
            raise RuntimeError("DATA_KEY must be valid hex or URL-safe base64") from exc
    if len(key) != 32:
        raise RuntimeError("DATA_KEY must be 32 bytes")
    return key


def validate_crypto_config() -> None:
    if os.environ.get("APP_ENV", "").casefold() != "production":
        return
    secret = os.environ.get("SESSION_SECRET", "")
    if len(secret) < 32 or secret == "change-me-to-a-long-random-string":
        raise RuntimeError("SESSION_SECRET must contain at least 32 characters in production")
    raw_key = os.environ.get("DATA_KEY", "").strip()
    if not raw_key:
        raise RuntimeError("DATA_KEY is required in production")
    _decode_data_key(raw_key)


def data_key() -> bytes:
    raw = os.environ.get("DATA_KEY", "").strip()
    if raw:
        return _decode_data_key(raw)
    secret = os.environ.get("SESSION_SECRET", "dev-only-secret").encode()
    return HKDF(algorithm=SHA256(), length=32, salt=None, info=b"todo-data-v1").derive(secret)


def login_lookup(normalized_login: str) -> str:
    return hmac.new(data_key(), b"login:" + normalized_login.encode(), hashlib.sha256).hexdigest()


def encrypt_text(value: str) -> str:
    nonce = secrets.token_bytes(12)
    blob = AESGCM(data_key()).encrypt(nonce, value.encode(), None)
    return _PREFIX + urlsafe_b64encode(nonce + blob).decode()


def decrypt_text(value: str) -> str:
    if not value.startswith(_PREFIX):
        return value
    raw = urlsafe_b64decode(value[len(_PREFIX) :] + "=" * (-len(value[len(_PREFIX) :]) % 4))
    return AESGCM(data_key()).decrypt(raw[:12], raw[12:], None).decode()
