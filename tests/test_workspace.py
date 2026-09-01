from fastapi.testclient import TestClient


def test_workspace_requires_authentication(client: TestClient):
    response = client.get("/api/workspace/me")
    assert response.status_code == 401


def test_student_workspace_is_self_scoped_and_alerts_restricted(client: TestClient, account):
    _, headers = account
    me = client.get("/api/workspace/me", headers=headers)
    assert me.status_code == 200
    payload = me.json()
    assert payload["scope"] == "self_only"
    assert payload["capabilities"]["content_workspace"] is False
    assert payload["capabilities"]["operations_workspace"] is False

    dashboard = client.get("/api/workspace/dashboard", headers=headers)
    assert dashboard.status_code == 200
    assert dashboard.json()["viewer"]["scope"] == "self_only"

    alerts = client.get("/api/workspace/alerts", headers=headers)
    assert alerts.status_code == 200
    assert alerts.json()["restricted"] is True
    assert alerts.json()["alerts"] == []


def test_workspace_time_range_validation(client: TestClient, account):
    _, headers = account
    response = client.get(
        "/api/workspace/dashboard?start_at=2026-09-02T00:00:00Z&end_at=2026-09-01T00:00:00Z",
        headers=headers,
    )
    assert response.status_code == 422


def test_student_tasks_do_not_expose_content_fields(client: TestClient, account):
    _, headers = account
    response = client.get("/api/workspace/tasks", headers=headers)
    assert response.status_code == 200
    payload = response.json()
    assert payload["privacy"]["student_text_and_images_hidden"] is True
    for item in payload["items"]:
        assert set(item).issubset({"task_type", "id", "status", "requires_review", "created_at", "updated_at", "error_code", "scope"})
        assert "question_text" not in item
        assert "image_key" not in item
