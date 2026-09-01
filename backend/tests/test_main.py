"""Smoke test: the app imports and boots."""

from fastapi.testclient import TestClient

from app.config import as_sqlalchemy_url
from app.main import app


def test_app_boots() -> None:
    client = TestClient(app)
    response = client.get("/openapi.json")
    assert response.status_code == 200


def test_as_sqlalchemy_url_adds_psycopg_dialect() -> None:
    assert as_sqlalchemy_url("postgres://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert as_sqlalchemy_url("postgresql://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    already = "postgresql+psycopg://u:p@h/db"
    assert as_sqlalchemy_url(already) == already
    sqlite = "sqlite:///./dev.db"
    assert as_sqlalchemy_url(sqlite) == sqlite
