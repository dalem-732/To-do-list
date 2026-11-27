import os
import time
from collections import OrderedDict, deque
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

MAX_BODY_BYTES = 8 * 1024
AUTH_PATHS = frozenset({"/auth/login", "/auth/register"})
_WRITE_METHODS = {b"POST", b"PUT", b"PATCH", b"DELETE"}
_MAX_RATE_BUCKETS = 10_000
_RAILWAY_MARKERS = ("RAILWAY_ENVIRONMENT_ID", "RAILWAY_PROJECT_ID", "RAILWAY_SERVICE_ID")
_STATIC_PREFIX = "/static/"
_CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "style-src 'self'; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "img-src 'self' data:; "
    "object-src 'none'; "
    "base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
)


def _headers(scope: Scope) -> dict[bytes, bytes]:
    return {name.lower(): value for name, value in scope.get("headers", [])}


def _env_flag(name: str) -> bool | None:
    value = os.environ.get(name, "").strip()
    if not value:
        return None
    return value == "1"


def trust_proxy_headers() -> bool:
    configured = _env_flag("TRUST_PROXY_HEADERS")
    if configured is not None:
        return configured
    return any(os.environ.get(name) for name in _RAILWAY_MARKERS)


def https_enabled() -> bool:
    configured = _env_flag("COOKIE_SECURE")
    if configured is not None:
        return configured
    return os.environ.get("APP_ENV", "").casefold() == "production"


def client_ip(scope: Scope) -> str:
    if trust_proxy_headers():
        headers = _headers(scope)
        real_ip = headers.get(b"x-real-ip", b"").decode("latin-1").strip()
        if real_ip:
            return real_ip[:64]
        forwarded = [
            part.strip() for part in headers.get(b"x-forwarded-for", b"").decode("latin-1").split(",") if part.strip()
        ]
        if forwarded:
            return forwarded[-1][:64]
    client = scope.get("client")
    if client and client[0]:
        return str(client[0])[:64]
    return "unknown"


def is_same_origin(headers: dict[bytes, bytes]) -> bool:
    if headers.get(b"sec-fetch-site", b"") == b"cross-site":
        return False
    origin = headers.get(b"origin")
    if not origin:
        return True
    try:
        origin_host = urlsplit(origin.decode("latin-1")).netloc.casefold()
    except ValueError:
        return False
    host = headers.get(b"host", b"").decode("latin-1").casefold()
    return bool(origin_host) and origin_host == host


class _RateBuckets:
    def __init__(self, limit: int, window: float) -> None:
        self.limit = limit
        self.window = window
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()

    def allow(self, key: str, now: float) -> bool:
        bucket = self._hits.setdefault(key, deque())
        self._hits.move_to_end(key)
        while bucket and now - bucket[0] > self.window:
            bucket.popleft()
        while len(self._hits) > _MAX_RATE_BUCKETS:
            self._hits.popitem(last=False)
        if len(bucket) >= self.limit:
            return False
        bucket.append(now)
        return True


class RateLimitMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.general = _RateBuckets(
            int(os.environ.get("RATE_LIMIT", "60")),
            float(os.environ.get("RATE_LIMIT_WINDOW", "60")),
        )
        self.auth = _RateBuckets(
            int(os.environ.get("AUTH_RATE_LIMIT", "10")),
            float(os.environ.get("AUTH_RATE_LIMIT_WINDOW", "300")),
        )

    async def _reject(self, buckets: _RateBuckets, scope: Scope, receive: Receive, send: Send) -> None:
        await Response(
            "too many requests",
            status_code=429,
            headers={"Retry-After": str(max(1, int(buckets.window)))},
        )(scope, receive, send)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path == "/health" or path.startswith(_STATIC_PREFIX):
            await self.app(scope, receive, send)
            return
        now = time.monotonic()
        key = client_ip(scope)
        if not self.general.allow(key, now):
            await self._reject(self.general, scope, receive, send)
            return
        if scope.get("method") == "POST" and path in AUTH_PATHS and not self.auth.allow(key, now):
            await self._reject(self.auth, scope, receive, send)
            return
        await self.app(scope, receive, send)


class BodyLimitMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method", "GET").encode() not in _WRITE_METHODS:
            await self.app(scope, receive, send)
            return

        chunks: list[bytes] = []
        size = 0
        while True:
            message: Message = await receive()
            if message["type"] != "http.request":
                continue
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > self.max_bytes:
                await Response("payload too large", status_code=413)(scope, receive, send)
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break

        body = b"".join(chunks)
        sent = False

        async def replay() -> Message:
            nonlocal sent
            if sent:
                return {"type": "http.request", "body": b"", "more_body": False}
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        await self.app(scope, replay, send)


class OriginCheckMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method", "GET").encode() not in _WRITE_METHODS:
            await self.app(scope, receive, send)
            return
        if not is_same_origin(_headers(scope)):
            await Response("forbidden", status_code=403)(scope, receive, send)
            return
        await self.app(scope, receive, send)


def security_headers(request: Request, response: Response) -> None:
    response.headers["Content-Security-Policy"] = _CONTENT_SECURITY_POLICY
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    if https_enabled():
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
    if request.url.path.startswith(_STATIC_PREFIX) and response.status_code < 400:
        if request.query_params.get("v"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers["Cache-Control"] = "no-cache"
    else:
        response.headers["Cache-Control"] = "no-store"


def wants_json(request: Request) -> bool:
    return "application/json" in request.headers.get("accept", "")
