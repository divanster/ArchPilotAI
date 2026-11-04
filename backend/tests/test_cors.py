import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.main import CORS_ALLOWED_METHODS, app

PROJECT = "00000000-0000-0000-0000-000000000001"
PLAN = "00000000-0000-0000-0000-000000000002"
PATCH_ID = "00000000-0000-0000-0000-000000000003"

# (method, path) pairs the frontend (frontend/src/api.ts) actually sends, covering phases 13A-14B.
FRONTEND_REQUESTS = [
    ("GET", f"/api/projects/{PROJECT}/git"),
    ("PUT", f"/api/projects/{PROJECT}/git"),
    ("PUT", f"/api/projects/{PROJECT}/git/pat"),
    ("DELETE", f"/api/projects/{PROJECT}/git/pat"),
    ("POST", f"/api/projects/{PROJECT}/git/test"),
    ("POST", f"/api/projects/{PROJECT}/git/clone"),
    ("POST", f"/api/projects/{PROJECT}/git/attach"),
    ("POST", f"/api/projects/{PROJECT}/git/push"),
    ("POST", f"/api/projects/{PROJECT}/git/patches/{PATCH_ID}/commit"),
    ("POST", f"/api/projects/{PROJECT}/git/patches/{PATCH_ID}/pull-request"),
    ("PUT", f"/api/projects/{PROJECT}/agent-hooks/lint"),
    ("PUT", f"/api/projects/{PROJECT}/agent-skills/review"),
    ("PUT", f"/api/projects/{PROJECT}/plans/{PLAN}/tasks/T1/checklist"),
    ("POST", f"/api/projects/{PROJECT}/plans/{PLAN}/tasks/T1/checklist/run-next"),
    ("PATCH", f"/api/projects/{PROJECT}"),
    ("DELETE", f"/api/projects/{PROJECT}"),
]


@pytest.fixture
def cors_client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def origin() -> str:
    return get_settings().cors_origins[0]


def _allowed(response_header: str) -> set[str]:
    return {m.strip() for m in response_header.split(",")}


@pytest.mark.parametrize(("method", "path"), FRONTEND_REQUESTS)
def test_preflight_allows_frontend_method(
    cors_client: TestClient, origin: str, method: str, path: str
) -> None:
    response = cors_client.options(
        path,
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": method,
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert response.status_code == 200, response.text
    assert response.headers["access-control-allow-origin"] == origin
    assert method in _allowed(response.headers["access-control-allow-methods"])
    assert "content-type" in response.headers["access-control-allow-headers"].lower()


def test_preflight_rejects_unknown_origin(cors_client: TestClient) -> None:
    response = cors_client.options(
        f"/api/projects/{PROJECT}/git",
        headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "PUT"},
    )
    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


def test_origins_are_not_wildcard() -> None:
    assert "*" not in get_settings().cors_origins


def test_every_route_method_is_cors_allowed() -> None:
    """A new endpoint with a method missing from CORS would break the browser silently."""
    used = {
        method
        for route in app.routes
        if isinstance(route, APIRoute)
        for method in route.methods or ()
        if method not in {"HEAD", "OPTIONS"}
    }
    assert used <= set(CORS_ALLOWED_METHODS), used - set(CORS_ALLOWED_METHODS)
