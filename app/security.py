import os
import time
from collections import OrderedDict, deque

from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

MAX_BODY_BYTES = 8 * 1024
_WRITE_METHODS = {b"POST", b"PUT", b"PATCH"}
_MAX_RATE_BUCKETS = 10_000


def client_ip(scope: Scope) -> str:
    headers = {name.lower(): value for name, value in scope.get("headers", [])}
    trust_proxy = os.environ.get("TRUST_PROXY_HEADERS") == "1"
    railway_request = b"x-railway-edge" in headers
    real_ip = headers.get(b"x-real-ip")
    if real_ip and (railway_request or trust_proxy):
        return real_ip.decode("latin-1").strip()[:64]
    forwarded = headers.get(b"x-forwarded-for")
    if forwarded and trust_proxy:
        parts = [part.strip() for part in forwarded.decode("latin-1").split(",") if part.strip()]
        if parts:
            return parts[0][:64]
    client = scope.get("client")
    if client and client[0]:
        return str(client[0])[:64]
    return "unknown"


class RateLimitMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.limit = int(os.environ.get("RATE_LIMIT", "60"))
        self.window = float(os.environ.get("RATE_LIMIT_WINDOW", "60"))
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path == "/health" or path.startswith("/static"):
            await self.app(scope, receive, send)
            return
        now = time.monotonic()
        key = client_ip(scope)
        bucket = self._hits.setdefault(key, deque())
        self._hits.move_to_end(key)
        while bucket and now - bucket[0] > self.window:
            bucket.popleft()
        while len(self._hits) > _MAX_RATE_BUCKETS:
            self._hits.popitem(last=False)
        if len(bucket) >= self.limit:
            await Response(
                "too many requests",
                status_code=429,
                headers={"Retry-After": str(max(1, int(self.window)))},
            )(scope, receive, send)
            return
        bucket.append(now)
        await self.app(scope, receive, send)


class BodyLimitMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method", "GET").encode() not in _WRITE_METHODS:
            await self.app(scope, receive, send)
            return

        body = b""
        while True:
            message: Message = await receive()
            if message["type"] != "http.request":
                continue
            body += message.get("body", b"")
            if len(body) > self.max_bytes:
                await Response("payload too large", status_code=413)(scope, receive, send)
                return
            if not message.get("more_body", False):
                break

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
        headers = {name.lower(): value for name, value in scope.get("headers", [])}
        origin = headers.get(b"origin")
        fetch_site = headers.get(b"sec-fetch-site", b"")
        host = headers.get(b"host", b"").decode("latin-1").casefold()
        if fetch_site == b"cross-site":
            await Response("forbidden", status_code=403)(scope, receive, send)
            return
        if origin:
            from urllib.parse import urlsplit

            try:
                origin_host = urlsplit(origin.decode("latin-1")).netloc.casefold()
            except ValueError:
                origin_host = ""
            if not origin_host or origin_host != host:
                await Response("forbidden", status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def security_headers(response: Response) -> None:
    # 95css и его шрифт отдаются с jsDelivr, галочки — через data-SVG.
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "style-src 'self' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
        "font-src 'self' https://cdn.jsdelivr.net https://fonts.gstatic.com; "
        "connect-src 'self' https://fonts.googleapis.com https://fonts.gstatic.com; "
        "img-src 'self' data:; "
        "base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"


def wants_json(request: Request) -> bool:
    return "application/json" in request.headers.get("accept", "")
