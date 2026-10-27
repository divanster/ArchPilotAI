import uuid

import pytest
from fastapi.testclient import TestClient


def _create(client: TestClient, **body: object) -> dict[str, object]:
    payload = {"name": "Synthetic Project", **body}
    response = client.post("/api/projects", json=payload)
    assert response.status_code == 201, response.text
    data: dict[str, object] = response.json()
    return data


def test_create_defaults(client: TestClient) -> None:
    data = _create(client)
    assert data["name"] == "Synthetic Project"
    assert data["description"] is None
    assert data["status"] == "draft"
    uuid.UUID(str(data["id"]))
    assert data["created_at"] and data["updated_at"]


def test_create_trims_name_and_accepts_fields(client: TestClient) -> None:
    data = _create(client, name="  Padded  ", description="desc", status="active")
    assert data["name"] == "Padded"
    assert data["description"] == "desc"
    assert data["status"] == "active"


def test_list_and_get(client: TestClient) -> None:
    assert client.get("/api/projects").json() == []
    first = _create(client, name="A")
    second = _create(client, name="B")
    listed = client.get("/api/projects").json()
    assert {p["id"] for p in listed} == {first["id"], second["id"]}
    got = client.get(f"/api/projects/{first['id']}")
    assert got.status_code == 200
    assert got.json() == first


def test_patch_partial_update(client: TestClient) -> None:
    created = _create(client, description="keep")
    response = client.patch(f"/api/projects/{created['id']}", json={"status": "archived"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "archived"
    assert body["name"] == "Synthetic Project"
    assert body["description"] == "keep"
    assert body["updated_at"] >= created["updated_at"]


def test_patch_can_clear_description(client: TestClient) -> None:
    created = _create(client, description="x")
    response = client.patch(f"/api/projects/{created['id']}", json={"description": None})
    assert response.status_code == 200
    assert response.json()["description"] is None


def test_delete(client: TestClient) -> None:
    created = _create(client)
    assert client.delete(f"/api/projects/{created['id']}").status_code == 204
    assert client.get(f"/api/projects/{created['id']}").status_code == 404
    assert client.delete(f"/api/projects/{created['id']}").status_code == 404


def test_not_found(client: TestClient) -> None:
    missing = uuid.uuid4()
    assert client.get(f"/api/projects/{missing}").status_code == 404
    assert client.patch(f"/api/projects/{missing}", json={"name": "x"}).status_code == 404


def test_invalid_id_is_422(client: TestClient) -> None:
    assert client.get("/api/projects/not-a-uuid").status_code == 422


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"name": ""},
        {"name": "   "},
        {"name": "x" * 201},
        {"name": "ok", "status": "deleted"},
        {"name": "ok", "unknown": 1},
        {"name": "ok", "description": "d" * 10_001},
    ],
)
def test_create_validation(client: TestClient, body: dict[str, object]) -> None:
    assert client.post("/api/projects", json=body).status_code == 422


@pytest.mark.parametrize(
    "body",
    [{"name": ""}, {"name": None}, {"status": None}, {"status": "nope"}, {"id": "x"}],
)
def test_update_validation(client: TestClient, body: dict[str, object]) -> None:
    created = _create(client)
    assert client.patch(f"/api/projects/{created['id']}", json=body).status_code == 422
