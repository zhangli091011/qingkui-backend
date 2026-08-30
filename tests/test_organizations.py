import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.models import ClassMembership, OrganizationInvite, SchoolMembership, User, UserRole


def _register(client: TestClient, prefix: str) -> tuple[dict, dict[str, str]]:
    username = f"{prefix}_{uuid.uuid4().hex[:8]}"
    response = client.post(
        "/api/auth/register",
        json={"username": username, "password": "organization-test-password-123", "nickname": username},
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    return payload, {"Authorization": f"Bearer {payload['access_token']}"}


def _set_admin(user_id: str) -> None:
    with SessionLocal() as db:
        user = db.get(User, user_id)
        assert user is not None
        user.role = UserRole.admin
        db.commit()


def test_organization_feature_is_closed_by_default(client: TestClient) -> None:
    _, headers = _register(client, "org_disabled")
    assert client.get("/api/organizations/me", headers=headers).status_code == 404


def test_school_class_invites_and_anonymous_teacher_overview(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(settings, "organizations_enabled", True)
    admin, admin_headers = _register(client, "org_admin")
    teacher, teacher_headers = _register(client, "org_teacher")
    student, student_headers = _register(client, "org_student")
    _, outsider_headers = _register(client, "org_outsider")
    _set_admin(admin["user"]["id"])

    school = client.post(
        "/api/organizations/schools",
        json={"name": "青葵测试中学", "code": f"school-{uuid.uuid4().hex[:8]}"},
        headers=admin_headers,
    )
    assert school.status_code == 201, school.text
    school_id = school.json()["id"]
    classroom = client.post(
        f"/api/organizations/schools/{school_id}/classes",
        json={"name": "高一一班", "grade": "高一", "academic_year": "2026-2027"},
        headers=admin_headers,
    )
    assert classroom.status_code == 201, classroom.text
    class_id = classroom.json()["id"]

    teacher_invite = client.post(
        f"/api/organizations/schools/{school_id}/invites",
        json={"class_id": class_id, "member_role": "teacher", "max_uses": 1, "expires_hours": 24},
        headers=admin_headers,
    )
    assert teacher_invite.status_code == 201, teacher_invite.text
    teacher_code = teacher_invite.json()["code"]
    joined_teacher = client.post(
        "/api/organizations/invites/redeem",
        json={"code": teacher_code},
        headers=teacher_headers,
    )
    assert joined_teacher.status_code == 200
    assert joined_teacher.json()["school_role"] == "teacher"

    assert client.post(
        f"/api/organizations/schools/{school_id}/classes",
        json={"name": "教师越权班", "academic_year": "2026-2027"},
        headers=teacher_headers,
    ).status_code == 403
    assert client.post(
        f"/api/organizations/schools/{school_id}/invites",
        json={"class_id": class_id, "member_role": "teacher", "max_uses": 1},
        headers=teacher_headers,
    ).status_code == 403

    student_invite = client.post(
        f"/api/organizations/schools/{school_id}/invites",
        json={"class_id": class_id, "member_role": "student", "max_uses": 2, "expires_hours": 24},
        headers=teacher_headers,
    )
    assert student_invite.status_code == 201, student_invite.text
    student_code = student_invite.json()["code"]
    joined_student = client.post(
        "/api/organizations/invites/redeem",
        json={"code": student_code},
        headers=student_headers,
    )
    assert joined_student.status_code == 200
    assert joined_student.json()["joined"] is True
    repeated = client.post(
        "/api/organizations/invites/redeem",
        json={"code": student_code},
        headers=student_headers,
    )
    assert repeated.status_code == 200
    assert repeated.json()["joined"] is False

    with SessionLocal() as db:
        invite = db.scalar(select(OrganizationInvite).where(OrganizationInvite.id == student_invite.json()["id"]))
        assert invite is not None
        assert invite.code_hash != student_code
        assert student_code not in str(invite.__dict__)
        assert invite.use_count == 1

    session = client.post(
        "/api/qa/sessions", json={"mode": "knowledge"}, headers=student_headers
    )
    assert session.status_code == 201
    question = client.post(
        f"/api/qa/sessions/{session.json()['id']}/messages",
        json={"content": "解释二次函数", "help_level": "approach"},
        headers={**student_headers, "Idempotency-Key": f"org-{uuid.uuid4()}"},
    )
    assert question.status_code == 200
    assert client.post(
        "/api/learning/events",
        json={"event_type": "viewed_node", "node_id": "quadratic_function", "event_data": {}},
        headers=student_headers,
    ).status_code == 201

    assert client.get(f"/api/organizations/classes/{class_id}/overview", headers=outsider_headers).status_code == 403
    overview = client.get(f"/api/organizations/classes/{class_id}/overview", headers=teacher_headers)
    assert overview.status_code == 200, overview.text
    body = overview.text
    values = overview.json()
    assert values["student_count"] == 1
    assert values["active_7d_students"] == 1
    assert values["questions"] == 1
    assert values["students"][0]["anonymous_id"]
    assert "user_id" not in values["students"][0]
    assert student["user"]["username"] not in body
    assert "quadratic_function" not in body
    assert "解释二次函数" not in body

    memberships = client.get("/api/organizations/me", headers=student_headers)
    assert memberships.status_code == 200
    assert memberships.json()["memberships"][0]["school"]["id"] == school_id

    left = client.delete(
        f"/api/organizations/schools/{school_id}/membership",
        headers=student_headers,
    )
    assert left.status_code == 204
    assert client.get("/api/organizations/me", headers=student_headers).json()["memberships"] == []
    with SessionLocal() as db:
        user = db.get(User, student["user"]["id"])
        school_membership = db.scalar(
            select(SchoolMembership).where(
                SchoolMembership.school_id == school_id,
                SchoolMembership.user_id == student["user"]["id"],
            )
        )
        class_membership = db.scalar(
            select(ClassMembership).where(
                ClassMembership.class_id == class_id,
                ClassMembership.user_id == student["user"]["id"],
            )
        )
        assert user is not None and user.tenant_id is None
        assert school_membership is not None and school_membership.status == "left"
        assert class_membership is not None and class_membership.status == "left"


def test_user_cannot_join_two_schools(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(settings, "organizations_enabled", True)
    admin, admin_headers = _register(client, "org_multi_admin")
    student, student_headers = _register(client, "org_multi_student")
    _set_admin(admin["user"]["id"])

    codes = []
    for index in range(2):
        school = client.post(
            "/api/organizations/schools",
            json={"name": f"隔离学校 {index}", "code": f"isolation-{uuid.uuid4().hex[:8]}"},
            headers=admin_headers,
        ).json()
        invite = client.post(
            f"/api/organizations/schools/{school['id']}/invites",
            json={"member_role": "student", "max_uses": 1, "expires_hours": 24},
            headers=admin_headers,
        )
        assert invite.status_code == 201
        codes.append(invite.json()["code"])

    assert client.post(
        "/api/organizations/invites/redeem", json={"code": codes[0]}, headers=student_headers
    ).status_code == 200
    blocked = client.post(
        "/api/organizations/invites/redeem", json={"code": codes[1]}, headers=student_headers
    )
    assert blocked.status_code == 409
    with SessionLocal() as db:
        user = db.get(User, student["user"]["id"])
        assert user is not None and user.tenant_id is not None
