import uuid

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.models import AuditLog, CreditCode, School, SchoolMembership, User, UserRole


def _register(client: TestClient, prefix: str) -> tuple[dict, dict[str, str]]:
    username = f"{prefix[:23]}_{uuid.uuid4().hex[:8]}"
    response = client.post(
        "/api/auth/register",
        json={"username": username, "password": "campaign-test-password-123", "nickname": username},
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


def test_credit_campaigns_are_closed_by_default(client: TestClient) -> None:
    _, headers = _register(client, "campaign_disabled")
    response = client.post("/api/credits/redeem", json={"code": "QKC-disabled-code"}, headers=headers)
    assert response.status_code == 404


def test_hashed_codes_enforce_user_and_campaign_limits(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(settings, "credit_campaigns_enabled", True)
    admin, admin_headers = _register(client, "campaign_admin")
    first, first_headers = _register(client, "campaign_first")
    second, second_headers = _register(client, "campaign_second")
    _, third_headers = _register(client, "campaign_third")
    _set_admin(admin["user"]["id"])
    created = client.post(
        "/api/admin/credits/campaigns",
        json={
            "name": "试点活动额度",
            "amount": 50,
            "max_redemptions": 2,
            "per_user_limit": 1,
            "code_count": 2,
            "code_max_uses": 2,
            "expires_hours": 24,
        },
        headers=admin_headers,
    )
    assert created.status_code == 201, created.text
    campaign = created.json()
    first_code, second_code = campaign["codes"]
    with SessionLocal() as db:
        codes = list(db.scalars(select(CreditCode).where(CreditCode.campaign_id == campaign["id"])))
        assert len(codes) == 2
        assert all(code.code_hash not in {first_code, second_code} for code in codes)
        audit = db.scalar(
            select(AuditLog).where(
                AuditLog.action == "credits.campaign_created",
                AuditLog.target_id == campaign["id"],
            )
        )
        assert audit is not None
        assert first_code not in str(audit.details) and second_code not in str(audit.details)

    first_before = client.get("/api/credits", headers=first_headers).json()["balance"]
    redeemed = client.post("/api/credits/redeem", json={"code": first_code}, headers=first_headers)
    assert redeemed.status_code == 200
    assert redeemed.json()["amount"] == 50
    assert redeemed.json()["balance"] == first_before + 50
    assert client.post(
        "/api/credits/redeem", json={"code": first_code}, headers=first_headers
    ).status_code == 409
    assert client.post(
        "/api/credits/redeem", json={"code": second_code}, headers=first_headers
    ).status_code == 409

    second_before = client.get("/api/credits", headers=second_headers).json()["balance"]
    assert client.post(
        "/api/credits/redeem", json={"code": second_code}, headers=second_headers
    ).status_code == 200
    assert client.get("/api/credits", headers=second_headers).json()["balance"] == second_before + 50
    assert client.post(
        "/api/credits/redeem", json={"code": first_code}, headers=third_headers
    ).status_code == 404

    campaigns = client.get("/api/admin/credits/campaigns", headers=admin_headers)
    assert campaigns.status_code == 200
    row = next(item for item in campaigns.json() if item["id"] == campaign["id"])
    assert row["redemption_count"] == 2
    ledger = client.get("/api/credits/ledger", headers=first_headers).json()
    assert ledger[0]["entry_type"] == "campaign_redemption"
    assert ledger[0]["amount"] == 50
    assert first["user"]["id"] != second["user"]["id"]


def test_school_campaign_requires_matching_active_membership(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(settings, "credit_campaigns_enabled", True)
    admin, admin_headers = _register(client, "school_campaign_admin")
    member, member_headers = _register(client, "school_campaign_member")
    _, outsider_headers = _register(client, "school_campaign_outsider")
    _set_admin(admin["user"]["id"])
    with SessionLocal() as db:
        school = School(name="额度测试学校", code=f"credits-{uuid.uuid4().hex[:8]}", created_by=admin["user"]["id"])
        db.add(school)
        db.flush()
        user = db.get(User, member["user"]["id"])
        assert user is not None
        user.tenant_id = school.id
        db.add(SchoolMembership(school_id=school.id, user_id=user.id, role="student"))
        db.commit()
        school_id = school.id
    created = client.post(
        "/api/admin/credits/campaigns",
        json={
            "name": "校内活动",
            "amount": 20,
            "school_id": school_id,
            "max_redemptions": 10,
            "code_count": 1,
            "code_max_uses": 10,
        },
        headers=admin_headers,
    )
    assert created.status_code == 201
    code = created.json()["codes"][0]
    assert client.post(
        "/api/credits/redeem", json={"code": code}, headers=outsider_headers
    ).status_code == 403
    assert client.post(
        "/api/credits/redeem", json={"code": code}, headers=member_headers
    ).status_code == 200
    with SessionLocal() as db:
        membership = db.scalar(
            select(SchoolMembership).where(
                SchoolMembership.school_id == school_id,
                SchoolMembership.user_id == member["user"]["id"],
            )
        )
        assert membership is not None
        membership.status = "inactive"
        db.commit()
    other_campaign = client.post(
        "/api/admin/credits/campaigns",
        json={
            "name": "第二次校内活动",
            "amount": 20,
            "school_id": school_id,
            "max_redemptions": 10,
            "code_count": 1,
            "code_max_uses": 10,
        },
        headers=admin_headers,
    ).json()
    assert client.post(
        "/api/credits/redeem", json={"code": other_campaign["codes"][0]}, headers=member_headers
    ).status_code == 403
