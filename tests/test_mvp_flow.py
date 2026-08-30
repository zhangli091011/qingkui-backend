from fastapi.testclient import TestClient
import json
from concurrent.futures import ThreadPoolExecutor

from app.services.ai import HELP_GUIDANCE, MODE_GUIDANCE, QUESTION_POLICY


def test_health(client: TestClient):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["ai_ready"] is True


def test_graph_qa_learning_and_credit_flow(client: TestClient, account):
    auth, headers = account
    assert auth["user"]["nickname"] == "测试同学"
    starting_balance = client.get("/api/credits", headers=headers).json()["balance"]

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
    assert result["balance"] == starting_balance - 1
    assert result["assistant_message"]["citations"][0]["node_id"] == "quadratic_function"
    assert result["assistant_message"]["structured_content"]["uncertain"] is False

    credits = client.get("/api/credits/ledger", headers=headers)
    assert credits.status_code == 200
    assert credits.json()[0]["entry_type"] == "qa_charge"
    assert credits.json()[0]["balance_after"] == starting_balance - 1

    summary = client.get("/api/learning/summary", headers=headers)
    assert summary.status_code == 200
    assert any(item["id"] == "quadratic_function" for item in summary.json()["recent"])


def test_subject_catalog_and_auto_routing(client: TestClient, account):
    _, headers = account
    subjects = client.get("/api/knowledge/subjects", headers=headers)
    assert subjects.status_code == 200
    assert subjects.json() == ["语文", "数学", "英语", "物理", "化学", "生物", "政治", "历史", "地理"]

    session = client.post("/api/qa/sessions", json={"mode": "knowledge"}, headers=headers)
    assert session.status_code == 201
    answer = client.post(
        f"/api/qa/sessions/{session.json()['id']}/messages",
        json={"content": "导数是什么？", "help_level": "approach"},
        headers=headers,
    )
    assert answer.status_code == 200
    assert answer.json()["subject"] == "数学"


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


def test_session_history_restore_delete_and_cross_user_isolation(client: TestClient, account):
    _, headers = account
    created = client.post("/api/qa/sessions", json={"mode": "knowledge"}, headers=headers)
    assert created.status_code == 201
    session_id = created.json()["id"]
    listed = client.get("/api/qa/sessions", headers=headers)
    assert listed.status_code == 200
    assert any(item["id"] == session_id for item in listed.json())
    restored = client.get(f"/api/qa/sessions/{session_id}", headers=headers)
    assert restored.status_code == 200

    other = client.post(
        "/api/auth/register",
        json={"username": "student_02", "password": "student-pass-456", "nickname": "另一个同学"},
    )
    other_headers = {"Authorization": f"Bearer {other.json()['access_token']}"}
    assert client.get(f"/api/qa/sessions/{session_id}", headers=other_headers).status_code == 404
    assert client.delete(f"/api/qa/sessions/{session_id}", headers=other_headers).status_code == 404
    assert client.delete(f"/api/qa/sessions/{session_id}", headers=headers).status_code == 204
    assert client.get(f"/api/qa/sessions/{session_id}", headers=headers).status_code == 404


def test_concurrent_credit_charges_never_go_below_zero(client: TestClient, account):
    _, headers = account
    session = client.post("/api/qa/sessions", json={"mode": "knowledge"}, headers=headers).json()
    session_id = session["id"]

    def send(_index: int):
        return client.post(
            f"/api/qa/sessions/{session_id}/messages",
            json={"content": "什么是二次函数？", "help_level": "approach"},
            headers=headers,
        ).status_code

    with ThreadPoolExecutor(max_workers=4) as pool:
        statuses = list(pool.map(send, range(4)))
    assert all(status in (200, 502, 503) for status in statuses)
    balance = client.get("/api/credits", headers=headers).json()["balance"]
    assert balance >= 0


def test_streaming_qa_emits_deltas_and_atomic_done_event(client: TestClient, account):
    _, headers = account
    session = client.post(
        "/api/qa/sessions",
        json={"mode": "knowledge", "knowledge_node_id": "quadratic_function"},
        headers=headers,
    )
    assert session.status_code == 201

    with client.stream(
        "POST",
        f"/api/qa/sessions/{session.json()['id']}/messages/stream",
        json={"content": "用一句话解释二次函数", "help_level": "approach"},
        headers=headers,
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = "".join(response.iter_text())

    assert "event: meta" in body
    assert "event: delta" in body
    assert "event: done" in body
    done_data = next(
        json.loads(line.removeprefix("data: "))
        for block in body.split("\n\n")
        if block.startswith("event: done")
        for line in block.splitlines()
        if line.startswith("data: ")
    )
    assert done_data["credits_charged"] == 1
    assert done_data["assistant_message"]["content"]
    assert done_data["assistant_message"]["citations"]


def test_casual_greeting_does_not_inject_an_unrelated_knowledge_node(client: TestClient, account):
    _, headers = account
    session = client.post("/api/qa/sessions", json={"mode": "knowledge"}, headers=headers)
    assert session.status_code == 201
    answer = client.post(
        f"/api/qa/sessions/{session.json()['id']}/messages",
        json={"content": "你好", "help_level": "approach"},
        headers=headers,
    )
    assert answer.status_code == 200
    result = answer.json()
    assert result["assistant_message"]["provider"] == "local"
    assert result["assistant_message"]["citations"] == []
    assert "你好" in result["assistant_message"]["content"]


def test_knowledge_mode_does_not_force_a_follow_up_question():
    assert "不要默认追加练习或理解检查" in MODE_GUIDANCE["knowledge"]
    assert "不要为了互动强行追问" in HELP_GUIDANCE["approach"]
    assert "默认不要以问题结尾" in QUESTION_POLICY
