import hashlib
import hmac
import json
import os
import re
import secrets
import time
import uuid
from base64 import urlsafe_b64decode, urlsafe_b64encode

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.crypto import decrypt_text, encrypt_text, login_lookup
from app.models import User, UserSession
from app.security import https_enabled

LOGIN_RE = re.compile(r"^[\w]{3,32}$", re.UNICODE)
SESSION_SECONDS = 14 * 24 * 60 * 60
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
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
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    try:
        if int(payload.get("exp", 0)) < time.time():
            return None
    except (TypeError, ValueError):
        return None
    return payload


def _uuid_field(payload: dict, name: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(payload[name]))
    except (KeyError, ValueError, TypeError):
        return None


def start_session(db: Session, user: User) -> str:
    expires_at = int(time.time()) + SESSION_SECONDS
    record = UserSession(user_id=user.id, expires_at=expires_at)
    db.add(record)
    db.flush()
    return _sign({"uid": str(user.id), "sid": str(record.id), "sv": user.session_version, "exp": expires_at})


def _session_from_request(request, db: Session) -> tuple[User, uuid.UUID | None] | None:
    payload = _unsign(request.cookies.get("session"))
    if payload is None:
        return None
    user_id = _uuid_field(payload, "uid")
    if user_id is None:
        return None
    user = db.get(User, user_id)
    if user is None:
        return None
    try:
        version = int(payload.get("sv", 0))
    except (TypeError, ValueError):
        return None
    if version != user.session_version:
        return None
    if "sid" not in payload:
        return user, None
    session_id = _uuid_field(payload, "sid")
    if session_id is None:
        return None
    record = db.get(UserSession, session_id)
    if record is None or record.user_id != user.id or record.expires_at < time.time():
        return None
    return user, session_id


def user_from_request(request, db: Session) -> User | None:
    found = _session_from_request(request, db)
    return found[0] if found else None


def end_session(request, db: Session) -> None:
    found = _session_from_request(request, db)
    if found is None:
        return
    user, session_id = found
    if session_id is None:
        user.session_version += 1
    else:
        db.execute(delete(UserSession).where(UserSession.id == session_id))


def end_all_sessions(db: Session, user: User) -> None:
    user.session_version += 1
    db.execute(delete(UserSession).where(UserSession.user_id == user.id))


def purge_expired_sessions(db: Session) -> None:
    db.execute(delete(UserSession).where(UserSession.expires_at < int(time.time())))


def make_guest_token(guest_id: uuid.UUID) -> str:
    return _sign({"gid": str(guest_id), "exp": int(time.time()) + SESSION_SECONDS})


def read_guest(token: str | None) -> uuid.UUID | None:
    payload = _unsign(token)
    if payload is None:
        return None
    return _uuid_field(payload, "gid")


def set_session_cookie(response, token: str) -> None:
    response.set_cookie(
        "session",
        token,
        max_age=SESSION_SECONDS,
        httponly=True,
        samesite="lax",
        secure=https_enabled(),
        path="/",
    )


def clear_session_cookie(response) -> None:
    response.delete_cookie("session", path="/", httponly=True, samesite="lax", secure=https_enabled())


def guest_id_from_request(request) -> uuid.UUID | None:
    return read_guest(request.cookies.get("guest"))


def set_guest_cookie(response, guest_id: uuid.UUID) -> None:
    response.set_cookie(
        "guest",
        make_guest_token(guest_id),
        max_age=SESSION_SECONDS,
        httponly=True,
        samesite="lax",
        secure=https_enabled(),
        path="/",
    )
