import os
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.db import normalize_database_url

_ISOLATED_ENV = (
    "COOKIE_SECURE",
    "DATA_KEY",
    "MAX_TASKS_PER_OWNER",
    "TRUST_PROXY_HEADERS",
    "RAILWAY_ENVIRONMENT_ID",
    "RAILWAY_PROJECT_ID",
    "RAILWAY_SERVICE_ID",
)


def _reset_postgres(url: str) -> None:
    engine = create_engine(normalize_database_url(url))
    try:
        with engine.begin() as connection:
            connection.execute(text("DROP SCHEMA public CASCADE"))
            connection.execute(text("CREATE SCHEMA public"))
    finally:
        engine.dispose()


@pytest.fixture
def database_url(tmp_path) -> str:
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        _reset_postgres(url)
        return url
    return f"sqlite+pysqlite:///{tmp_path / 'test.db'}"


@pytest.fixture
def client(database_url, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("RATE_LIMIT", "100000")
    monkeypatch.setenv("AUTH_RATE_LIMIT", "100000")
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret")
    monkeypatch.setenv("APP_ENV", "test")
    for name in _ISOLATED_ENV:
        monkeypatch.delenv(name, raising=False)
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client
