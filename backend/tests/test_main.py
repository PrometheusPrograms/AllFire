"""Smoke test: the app imports and boots."""

from fastapi.testclient import TestClient

from app.main import app


def test_app_boots() -> None:
    client = TestClient(app)
    response = client.get("/openapi.json")
    assert response.status_code == 200
