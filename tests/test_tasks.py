import uuid

from fastapi.testclient import TestClient


def test_add_toggle_delete_and_keep_after_reload(client: TestClient) -> None:
    created = client.post("/tasks", data={"title": "  Купить молоко  "}, follow_redirects=False)
    assert created.status_code == 303
    assert created.headers["location"] == "/"

    page = client.get("/")
    assert page.status_code == 200
    assert "Купить молоко" in page.text
    assert 'id="empty" hidden' in page.text

    task_id = page.text.split('action="/tasks/')[1].split("/edit")[0]
    uuid.UUID(task_id)

    toggled = client.post(f"/tasks/{task_id}/toggle", follow_redirects=False)
    assert toggled.status_code == 303
    marked = client.get("/")
    assert 'class="task-item done"' in marked.text

    removed = client.post(f"/tasks/{task_id}/delete", follow_redirects=False)
    assert removed.status_code == 303
    emptied = client.get("/")
    assert "Купить молоко" not in emptied.text
    assert '<p id="empty">Список пуст.</p>' in emptied.text


def test_json_create_returns_task(client: TestClient) -> None:
    response = client.post(
        "/tasks",
        json={"title": "Написать отчёт"},
        headers={"Accept": "application/json"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["title"] == "Написать отчёт"
    assert body["done"] is False
    assert "Написать отчёт" in client.get("/").text


def test_reject_empty_title(client: TestClient) -> None:
    html = client.post("/tasks", data={"title": "   "})
    assert html.status_code == 422
    assert "Введите название задачи." in html.text

    payload = client.post(
        "/tasks",
        json={"title": ""},
        headers={"Accept": "application/json"},
    )
    assert payload.status_code == 422
    assert payload.json()["detail"] == "Введите название задачи."


def test_reject_long_title_and_control_characters(client: TestClient) -> None:
    long_title = "а" * 201
    too_long = client.post(
        "/tasks",
        json={"title": long_title},
        headers={"Accept": "application/json"},
    )
    assert too_long.status_code == 422
    assert too_long.json()["detail"] == "Название не длиннее 200 символов."

    controls = client.post(
        "/tasks",
        json={"title": "строка\nещё"},
        headers={"Accept": "application/json"},
    )
    assert controls.status_code == 422
    assert controls.json()["detail"] == "Название содержит недопустимые символы."
    assert "строка" not in client.get("/").text


def test_escapes_html_in_title(client: TestClient) -> None:
    client.post("/tasks", data={"title": "<script>alert(1)</script>"}, follow_redirects=True)
    page = client.get("/")
    assert "<script>alert" not in page.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page.text


def test_edit_title(client: TestClient) -> None:
    created = client.post(
        "/tasks",
        json={"title": "Старое название"},
        headers={"Accept": "application/json"},
    )
    task_id = created.json()["id"]
    updated = client.post(
        f"/tasks/{task_id}/edit",
        json={"title": "  Новое название  "},
        headers={"Accept": "application/json"},
    )
    assert updated.status_code == 200
    assert updated.json()["title"] == "Новое название"
    assert "Новое название" in client.get("/").text
    assert "Старое название" not in client.get("/").text


def test_json_toggle_and_delete(client: TestClient) -> None:
    created = client.post(
        "/tasks",
        json={"title": "Проверить отметку"},
        headers={"Accept": "application/json"},
    )
    task_id = created.json()["id"]

    toggled = client.post(f"/tasks/{task_id}/toggle", headers={"Accept": "application/json"})
    assert toggled.status_code == 200
    assert toggled.json()["done"] is True

    removed = client.post(f"/tasks/{task_id}/delete", headers={"Accept": "application/json"})
    assert removed.status_code == 200
    assert "Проверить отметку" not in client.get("/").text


def test_missing_task(client: TestClient) -> None:
    missing = client.post(
        f"/tasks/{uuid.uuid4()}/toggle",
        headers={"Accept": "application/json"},
    )
    assert missing.status_code == 404


def test_health(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_security_headers_and_body_limit(client: TestClient) -> None:
    page = client.get("/")
    assert "cdn.jsdelivr.net" not in page.text
    assert "fonts.googleapis.com" not in page.text
    assert "/static/vendor/95css/95css.min.css?v=" in page.text
    assert "/static/vendor/press-start-2p/press-start-2p.css?v=" in page.text
    policy = page.headers["content-security-policy"]
    assert "default-src 'self'" in policy
    assert "https://" not in policy
    assert page.headers["cache-control"] == "no-store"
    assert page.headers["x-content-type-options"] == "nosniff"
    assert page.headers["x-frame-options"] == "DENY"
    assert page.headers["referrer-policy"] == "no-referrer"

    oversized = client.post(
        "/tasks",
        content=b"title=" + b"a" * 9000,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert oversized.status_code == 413


def test_database_url_normalization() -> None:
    from app.db import normalize_database_url

    assert normalize_database_url("postgres://user:pass@host/db") == "postgresql+psycopg://user:pass@host/db"
    assert normalize_database_url("postgresql://user:pass@host/db") == "postgresql+psycopg://user:pass@host/db"


def test_cross_site_write_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/tasks",
        json={"title": "Чужой запрос"},
        headers={
            "Accept": "application/json",
            "Origin": "https://attacker.example",
            "Sec-Fetch-Site": "cross-site",
        },
    )
    assert response.status_code == 403
    assert "Чужой запрос" not in client.get("/").text


def test_schema_migration_is_recorded(client: TestClient) -> None:
    from sqlalchemy import text

    from app.db import _Session

    session = _Session()
    try:
        versions = set(session.scalars(text("SELECT version FROM schema_migrations")))
    finally:
        session.close()
    assert {"001_current_schema", "002_user_sessions"} <= versions


def test_client_ip_ignores_spoofable_headers_by_default(monkeypatch) -> None:
    from app.security import client_ip

    for name in ("TRUST_PROXY_HEADERS", "RAILWAY_ENVIRONMENT_ID", "RAILWAY_PROJECT_ID", "RAILWAY_SERVICE_ID"):
        monkeypatch.delenv(name, raising=False)
    scope = {
        "headers": [
            (b"x-forwarded-for", b"198.51.100.9"),
            (b"x-railway-edge", b"fra1"),
            (b"x-real-ip", b"203.0.113.7"),
        ],
        "client": ("127.0.0.1", 1234),
    }
    assert client_ip(scope) == "127.0.0.1"

    monkeypatch.setenv("RAILWAY_PROJECT_ID", "project")
    assert client_ip(scope) == "203.0.113.7"

    monkeypatch.setenv("TRUST_PROXY_HEADERS", "0")
    assert client_ip(scope) == "127.0.0.1"

    monkeypatch.setenv("TRUST_PROXY_HEADERS", "1")
    scope["headers"] = [(b"x-forwarded-for", b"10.0.0.1, 198.51.100.9")]
    assert client_ip(scope) == "198.51.100.9"
