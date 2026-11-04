from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("RATE_LIMIT", "1000")
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret")
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client
