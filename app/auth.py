import hashlib
import hmac
import json
import os
import re
import secrets
import time
import uuid
from base64 import urlsafe_b64decode, urlsafe_b64encode
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.crypto import decrypt_text, encrypt_text, login_lookup
from app.models import User

LOGIN_RE = re.compile(r"^[\w]{3,32}$", re.UNICODE)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_SESSION_DAYS = 14
_SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}
_FALLBACK_SECRET = secrets.token_hex(32)
_DUMMY_HASH = None


def normalize_login(login: str) -> str:
    return login.strip().casefold()


def validate_login(login: str) -> str:
    cleaned = normalize_login(login)
    if not LOGIN_RE.fullmatch(cleaned) or _CONTROL.search(login):
        raise ValueError("Логин: от 3 до 32 символов, буквы, цифры и _.")
    return cleaned


def validate_password(password: str) -> str:
    if not isinstance(password, str) or _CONTROL.search(password):
        raise ValueError("Пароль содержит недопустимые символы.")
    if len(password) < 8 or len(password) > 128:
        raise ValueError("Пароль: от 8 до 128 символов.")
    return password


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return f"{salt.hex()}:{digest.hex()}"


def _dummy_hash() -> str:
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password("not-a-real-password")
    return _DUMMY_HASH


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, digest_hex = stored.split(":", 1)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
        actual = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def check_password(password: str, stored: str | None) -> bool:
    return verify_password(password, stored or _dummy_hash())


def display_login(user: User) -> str:
    if user.login_enc:
        return decrypt_text(user.login_enc)
    return user.login


def encrypted_login(login: str) -> tuple[str, str]:
    return login_lookup(login), encrypt_text(login)


def find_user(db: Session, login: str) -> User | None:
    return db.scalar(select(User).where(User.login == login_lookup(login)))


def _secret() -> bytes:
    return os.environ.get("SESSION_SECRET", _FALLBACK_SECRET).encode()


def _sign(payload: dict) -> str:
    body = urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    signature = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{signature}"


def _unsign(token: str | None) -> dict | None:
    if not token or "." not in token:
        return None
    body, signature = token.split(".", 1)
    expected = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return None
    padded = body + "=" * (-len(body) % 4)
    try:
        payload = json.loads(urlsafe_b64decode(padded))
    except (ValueError, json.JSONDecodeError):
        return None
    try:
        if int(payload.get("exp", 0)) < time.time():
            return None
    except (TypeError, ValueError):
        return None
    return payload


def make_session(user_id: uuid.UUID) -> str:
    return _sign({"uid": str(user_id), "exp": int(time.time()) + _SESSION_DAYS * 24 * 60 * 60})


def read_session(token: str | None) -> uuid.UUID | None:
    payload = _unsign(token)
    if payload is None or "uid" not in payload:
        return None
    try:
        return uuid.UUID(payload["uid"])
    except (ValueError, TypeError):
        return None


def make_guest_token(guest_id: uuid.UUID) -> str:
    return _sign({"gid": str(guest_id), "exp": int(time.time()) + _SESSION_DAYS * 24 * 60 * 60})


def read_guest(token: str | None) -> uuid.UUID | None:
    payload = _unsign(token)
    if payload is None or "gid" not in payload:
        return None
    try:
        return uuid.UUID(payload["gid"])
    except (ValueError, TypeError):
        return None


def user_from_request(request, db: Session) -> User | None:
    user_id = read_session(request.cookies.get("session"))
    if user_id is None:
        return None
    return db.get(User, user_id)


def same_origin(request) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        return request.headers.get("sec-fetch-site", "") != "cross-site"
    try:
        origin_host = urlsplit(origin).netloc.casefold()
    except ValueError:
        return False
    expected_host = request.headers.get("host", "").casefold()
    return bool(origin_host and expected_host) and hmac.compare_digest(origin_host, expected_host)


def _secure_cookies() -> bool:
    configured = os.environ.get("COOKIE_SECURE")
    if configured is not None:
        return configured == "1"
    return os.environ.get("APP_ENV", "").casefold() == "production"


def set_session_cookie(response, user_id: uuid.UUID) -> None:
    response.set_cookie(
        "session",
        make_session(user_id),
        max_age=_SESSION_DAYS * 24 * 60 * 60,
        httponly=True,
        samesite="lax",
        secure=_secure_cookies(),
        path="/",
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie("session", path="/")


def guest_id_from_request(request) -> uuid.UUID | None:
    return read_guest(request.cookies.get("guest"))


def set_guest_cookie(response, guest_id: uuid.UUID) -> None:
    response.set_cookie(
        "guest",
        make_guest_token(guest_id),
        max_age=_SESSION_DAYS * 24 * 60 * 60,
        httponly=True,
        samesite="lax",
        secure=_secure_cookies(),
        path="/",
    )
