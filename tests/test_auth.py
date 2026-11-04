from fastapi.testclient import TestClient


def test_register_login_logout_and_reject_duplicate(client: TestClient) -> None:
    created = client.post("/auth/register", json={"login": "Ark_1", "password": "secret-pass"})
    assert created.status_code == 201
    assert created.json()["login"] == "ark_1"
    assert "session" in created.cookies

    me = client.get("/auth/me")
    assert me.status_code == 200
    assert me.json()["login"] == "ark_1"
    assert 'data-login="ark_1"' in client.get("/").text

    duplicate = client.post("/auth/register", json={"login": "ARK_1", "password": "another-pass"})
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == "Пользователь с таким логином уже существует."

    client.post("/auth/logout")
    assert client.get("/auth/me").status_code == 401

    wrong = client.post("/auth/login", json={"login": "ark_1", "password": "wrong-pass"})
    assert wrong.status_code == 401
    assert wrong.json()["detail"] == "Неверный логин или пароль."

    logged = client.post("/auth/login", json={"login": "ark_1", "password": "secret-pass"})
    assert logged.status_code == 200
    assert logged.json()["login"] == "ark_1"


def test_guest_tasks_follow_the_browser_and_then_the_account(client: TestClient) -> None:
    from app.main import app

    client.get("/")
    created = client.post("/tasks", json={"title": "Только этот браузер"}, headers={"Accept": "application/json"})
    assert created.status_code == 201
    assert "Только этот браузер" in client.get("/").text

    with TestClient(app) as other:
        assert "Только этот браузер" not in other.get("/").text

    client.post("/auth/register", json={"login": "owner", "password": "secret-pass"})
    assert "Только этот браузер" in client.get("/").text
    client.post("/auth/logout")
    assert "Только этот браузер" not in client.get("/").text
    client.post("/auth/login", json={"login": "owner", "password": "secret-pass"})
    assert "Только этот браузер" in client.get("/").text


def test_login_and_task_text_are_encrypted_at_rest(client: TestClient) -> None:
    from sqlalchemy import select

    from app.db import _Session
    from app.models import Task, User

    client.post("/auth/register", json={"login": "cipher", "password": "secret-pass"})
    client.post("/tasks", json={"title": "Скрытая задача"}, headers={"Accept": "application/json"})
    session = _Session()
    try:
        user = session.scalar(select(User).where(User.login_enc.is_not(None)))
        task = session.scalar(select(Task).where(Task.user_id == user.id))
        assert user.login != "cipher"
        assert user.login_enc.startswith("enc1:")
        assert "cipher" not in user.login_enc
        assert task.title.startswith("enc1:")
        assert "Скрытая" not in task.title
    finally:
        session.close()
    assert "Скрытая задача" in client.get("/").text


def test_register_rejects_short_password(client: TestClient) -> None:
    response = client.post("/auth/register", json={"login": "neo", "password": "short"})
    assert response.status_code == 422
    assert "Пароль" in response.json()["detail"]


def test_production_requires_persistent_secrets(monkeypatch) -> None:
    from app.crypto import validate_crypto_config

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    monkeypatch.delenv("DATA_KEY", raising=False)
    try:
        validate_crypto_config()
    except RuntimeError as exc:
        assert "SESSION_SECRET" in str(exc)
    else:
        raise AssertionError("Production started without SESSION_SECRET")

    monkeypatch.setenv("SESSION_SECRET", "s" * 32)
    try:
        validate_crypto_config()
    except RuntimeError as exc:
        assert "DATA_KEY" in str(exc)
    else:
        raise AssertionError("Production started without DATA_KEY")

    monkeypatch.setenv("DATA_KEY", "ab" * 32)
    validate_crypto_config()
