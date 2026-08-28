from fastapi.testclient import TestClient


def test_health(client: TestClient):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["ai_ready"] is True


def test_graph_qa_learning_and_credit_flow(client: TestClient, account):
    auth, headers = account
    assert auth["user"]["nickname"] == "测试同学"

    search = client.get("/api/knowledge/search", params={"q": "二次函数"}, headers=headers)
    assert search.status_code == 200
    assert search.json()[0]["id"] == "quadratic_function"

    neighbors = client.get("/api/knowledge/nodes/quadratic_function/neighbors", headers=headers)
    assert neighbors.status_code == 200
    assert len(neighbors.json()["nodes"]) >= 3

    session = client.post(
        "/api/qa/sessions",
        json={"mode": "knowledge", "knowledge_node_id": "quadratic_function"},
        headers=headers,
    )
    assert session.status_code == 201
    session_id = session.json()["id"]

    answer = client.post(
        f"/api/qa/sessions/{session_id}/messages",
        json={"content": "什么是二次函数？", "help_level": "approach"},
        headers=headers,
    )
    assert answer.status_code == 200, answer.text
    result = answer.json()
    assert result["credits_charged"] == 1
    assert result["balance"] == 1279
    assert result["assistant_message"]["citations"][0]["node_id"] == "quadratic_function"
    assert result["assistant_message"]["structured_content"]["uncertain"] is False

    credits = client.get("/api/credits/ledger", headers=headers)
    assert credits.status_code == 200
    assert credits.json()[0]["entry_type"] == "qa_charge"
    assert credits.json()[0]["balance_after"] == 1279

    summary = client.get("/api/learning/summary", headers=headers)
    assert summary.status_code == 200
    assert any(item["id"] == "quadratic_function" for item in summary.json()["recent"])


def test_verified_requires_evidence(client: TestClient, account):
    _, headers = account
    direct = client.patch(
        "/api/learning/nodes/discriminant/state",
        json={"status": "verified"},
        headers=headers,
    )
    assert direct.status_code == 409

    event = client.post(
        "/api/learning/events",
        json={"event_type": "completed_check", "node_id": "discriminant", "event_data": {"passed": True}},
        headers=headers,
    )
    assert event.status_code == 201
    summary = client.get("/api/learning/summary", headers=headers).json()
    item = next(item for item in summary["recent"] if item["id"] == "discriminant")
    assert item["status"] == "verified"


def test_refresh_rotates_session(client: TestClient, account):
    auth, _ = account
    refreshed = client.post("/api/auth/refresh", json={"refresh_token": auth["refresh_token"]})
    assert refreshed.status_code == 200
    assert refreshed.json()["refresh_token"] != auth["refresh_token"]
    reused = client.post("/api/auth/refresh", json={"refresh_token": auth["refresh_token"]})
    assert reused.status_code == 401
