import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


TEST_DB = Path(__file__).parent / "qingkui-test.db"
if TEST_DB.exists():
    TEST_DB.unlink()
os.environ["APP_ENV"] = "test"
os.environ["DATABASE_URL"] = f"sqlite:///{TEST_DB.as_posix()}"
os.environ["JWT_SECRET"] = "test-secret-that-is-long-enough-for-tests"
os.environ["AI_PROVIDER"] = "stub"

from app.main import app  # noqa: E402
from app.db import engine  # noqa: E402


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as test_client:
        yield test_client
    engine.dispose()
    if TEST_DB.exists():
        TEST_DB.unlink()


@pytest.fixture
def account(client: TestClient):
    username = "student_01"
    response = client.post(
        "/api/auth/register",
        json={"username": username, "password": "student-pass-123", "nickname": "测试同学"},
    )
    if response.status_code == 409:
        response = client.post(
            "/api/auth/login",
            json={"username": username, "password": "student-pass-123"},
        )
    assert response.status_code == 200 or response.status_code == 201
    payload = response.json()
    return payload, {"Authorization": f"Bearer {payload['access_token']}"}
