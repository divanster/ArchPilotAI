from fastapi.testclient import TestClient

from app.main import app


def test_health_returns_typed_ok() -> None:
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "ArchPilotAI"
    assert set(body) == {"status", "service", "environment", "version"}
