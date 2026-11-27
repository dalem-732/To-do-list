import time
import uuid
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

JSON = {"Accept": "application/json"}


def _register(client: TestClient, login: str) -> None:
    response = client.post("/auth/register", json={"login": login, "password": "secret-pass"})
    assert response.status_code == 201


def _db():
    from app.db import _Session

    return _Session()


def test_tasks_are_isolated_between_users(client: TestClient) -> None:
    from app.main import app

    _register(client, "alice")
    task_id = client.post("/tasks", json={"title": "Задача Алисы"}, headers=JSON).json()["id"]

    with TestClient(app) as other:
        _register(other, "bob")
        assert other.get("/tasks").json() == []
        assert "Задача Алисы" not in other.get("/").text
        for action in ("toggle", "delete"):
            assert other.post(f"/tasks/{task_id}/{action}", headers=JSON).status_code == 404
        edited = other.post(f"/tasks/{task_id}/edit", json={"title": "Взлом"}, headers=JSON)
        assert edited.status_code == 404

    with TestClient(app) as guest:
        guest.get("/")
        assert guest.post(f"/tasks/{task_id}/toggle", headers=JSON).status_code == 404

    tasks = client.get("/tasks").json()
    assert [task["title"] for task in tasks] == ["Задача Алисы"]
    assert tasks[0]["done"] is False


def test_task_list_uses_the_same_format_as_other_endpoints(client: TestClient) -> None:
    created = client.post("/tasks", json={"title": "Формат"}, headers=JSON).json()
    listed = client.get("/tasks").json()
    assert listed == [created]
    assert set(created) == {"id", "title", "done", "created_at"}


def test_origin_check_middleware(client: TestClient) -> None:
    foreign = client.post("/tasks", json={"title": "Чужой"}, headers={**JSON, "Origin": "https://evil.example"})
    assert foreign.status_code == 403
    null_origin = client.post("/tasks", json={"title": "Нулевой"}, headers={**JSON, "Origin": "null"})
    assert null_origin.status_code == 403
    login = client.post(
        "/auth/login",
        json={"login": "nobody", "password": "secret-pass"},
        headers={"Origin": "https://evil.example"},
    )
    assert login.status_code == 403
    same = client.post("/tasks", json={"title": "Свой"}, headers={**JSON, "Origin": "http://testserver"})
    assert same.status_code == 201
    titles = [task["title"] for task in client.get("/tasks").json()]
    assert titles == ["Свой"]


def test_logout_revokes_the_session_cookie(client: TestClient) -> None:
    from app.main import app

    _register(client, "carol")
    token = client.cookies.get("session")
    assert client.post("/auth/logout").status_code == 200
    assert client.get("/auth/me").status_code == 401

    with TestClient(app) as replay:
        replay.cookies.set("session", token)
        assert replay.get("/auth/me").status_code == 401


def test_logout_keeps_other_devices_and_logout_all_ends_them(client: TestClient) -> None:
    from app.main import app

    _register(client, "dave")
    with TestClient(app) as phone, TestClient(app) as laptop:
        assert phone.post("/auth/login", json={"login": "dave", "password": "secret-pass"}).status_code == 200
        assert laptop.post("/auth/login", json={"login": "dave", "password": "secret-pass"}).status_code == 200
        client.post("/auth/logout")
        assert phone.get("/auth/me").status_code == 200
        assert phone.post("/auth/logout-all").status_code == 200
        assert phone.get("/auth/me").status_code == 401
        assert laptop.get("/auth/me").status_code == 401
        assert laptop.post("/auth/logout-all").status_code == 401
        assert laptop.post("/auth/login", json={"login": "dave", "password": "secret-pass"}).status_code == 200
        assert laptop.get("/auth/me").status_code == 200


def test_legacy_session_cookie_still_works_until_logout(client: TestClient) -> None:
    from sqlalchemy import select

    from app.auth import SESSION_SECONDS, _sign
    from app.main import app
    from app.models import User

    _register(client, "erin")
    session = _db()
    try:
        user = session.scalar(select(User))
        legacy = _sign({"uid": str(user.id), "exp": int(time.time()) + SESSION_SECONDS})
    finally:
        session.close()

    with TestClient(app) as old_browser:
        old_browser.cookies.set("session", legacy)
        assert old_browser.get("/auth/me").json()["login"] == "erin"
        old_browser.post("/auth/logout")
        old_browser.cookies.set("session", legacy)
        assert old_browser.get("/auth/me").status_code == 401


def test_task_cap_per_owner(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("MAX_TASKS_PER_OWNER", "2")
    for index in range(2):
        assert client.post("/tasks", json={"title": f"Задача {index}"}, headers=JSON).status_code == 201
    blocked = client.post("/tasks", json={"title": "Лишняя"}, headers=JSON)
    assert blocked.status_code == 409
    assert "2" in blocked.json()["detail"]
    html = client.post("/tasks", data={"title": "Лишняя"})
    assert html.status_code == 409
    assert "Достигнут лимит" in html.text


def test_expired_guest_tasks_are_removed(client: TestClient) -> None:
    from sqlalchemy import select

    from app.cleanup import run_cleanup
    from app.crypto import decrypt_text, encrypt_text
    from app.models import Task

    _register(client, "frank")
    client.post("/tasks", json={"title": "Аккаунт"}, headers=JSON)
    old = datetime.now(UTC) - timedelta(days=30)
    session = _db()
    try:
        session.add(Task(title=encrypt_text("Старый гость"), guest_id=uuid.uuid4(), created_at=old))
        session.add(Task(title=encrypt_text("Свежий гость"), guest_id=uuid.uuid4()))
        session.add(Task(title=encrypt_text("Без владельца"), created_at=old))
        for task in session.scalars(select(Task).where(Task.user_id.is_not(None))):
            task.created_at = old
        session.commit()
        run_cleanup(session)
        titles = sorted(decrypt_text(task.title) for task in session.scalars(select(Task)))
    finally:
        session.close()
    assert titles == ["Аккаунт", "Без владельца", "Свежий гость"]


def test_unreadable_ciphertext_does_not_break_the_page(client: TestClient) -> None:
    from sqlalchemy import select

    from app.crypto import UNREADABLE
    from app.models import Task

    client.post("/tasks", json={"title": "Будет испорчена"}, headers=JSON)
    session = _db()
    try:
        task = session.scalar(select(Task))
        task.title = "enc1:" + "A" * 40
        session.commit()
    finally:
        session.close()
    page = client.get("/")
    assert page.status_code == 200
    assert UNREADABLE in page.text
    assert client.get("/tasks").json()[0]["title"] == UNREADABLE


def test_static_assets_are_cacheable_and_hsts_in_production(client: TestClient, monkeypatch) -> None:
    from app.main import static_url

    versioned = client.get(static_url("app.js"))
    assert versioned.status_code == 200
    assert versioned.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert client.get("/static/app.js").headers["cache-control"] == "no-cache"
    assert "strict-transport-security" not in client.get("/").headers

    monkeypatch.setenv("COOKIE_SECURE", "1")
    assert client.get("/").headers["strict-transport-security"] == "max-age=31536000"
    for asset in (
        "vendor/95css/95css.min.css",
        "vendor/press-start-2p/press-start-2p.css",
        "vendor/press-start-2p/press-start-2p-cyrillic.woff2",
    ):
        assert client.get(f"/static/{asset}").status_code == 200


def _limited_client(monkeypatch, general: int, auth: int) -> TestClient:
    from app.security import RateLimitMiddleware

    for name in ("TRUST_PROXY_HEADERS", "RAILWAY_ENVIRONMENT_ID", "RAILWAY_PROJECT_ID", "RAILWAY_SERVICE_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("RATE_LIMIT", str(general))
    monkeypatch.setenv("AUTH_RATE_LIMIT", str(auth))

    async def ok(_request):
        return PlainTextResponse("ok")

    inner = Starlette(routes=[Route("/tasks", ok, methods=["GET"]), Route("/auth/login", ok, methods=["POST"])])
    return TestClient(RateLimitMiddleware(inner))


def test_spoofed_proxy_headers_do_not_bypass_rate_limit(monkeypatch) -> None:
    client = _limited_client(monkeypatch, general=5, auth=100)
    codes = [
        client.get("/tasks", headers={"x-railway-edge": "fra1", "x-real-ip": str(uuid.uuid4())}).status_code
        for _ in range(8)
    ]
    assert codes == [200] * 5 + [429] * 3


def test_auth_endpoints_have_a_stricter_limit(monkeypatch) -> None:
    client = _limited_client(monkeypatch, general=100, auth=3)
    logins = [client.post("/auth/login").status_code for _ in range(5)]
    assert logins == [200, 200, 200, 429, 429]
    assert client.get("/tasks").status_code == 200
