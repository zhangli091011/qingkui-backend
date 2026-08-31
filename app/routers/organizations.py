from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Response, status
from sqlalchemy import func, select, update

from app.config import settings
from app.deps import CurrentUser, DbSession, SuperAdminUser
from app.models import (
    AuditLog,
    ClassMembership,
    Conversation,
    KnowledgeNode,
    KnowledgeStatus,
    LearningEvent,
    Message,
    MistakeProblem,
    MistakePracticeRound,
    OrganizationInvite,
    PilotEnrollmentApproval,
    School,
    SchoolClass,
    SchoolMembership,
    User,
    UserKnowledgeState,
    UserRole,
)
from app.schemas import (
    ClassOverviewResponse,
    ClassStudentOverview,
    OrganizationInviteCreate,
    OrganizationInviteRedeem,
    OrganizationInviteResponse,
    OrganizationJoinResponse,
    OrganizationMeResponse,
    PilotEnrollmentApprovalCreate,
    PilotEnrollmentApprovalResponse,
    SchoolClassCreate,
    SchoolClassResponse,
    SchoolCreate,
    SchoolMembershipResponse,
    SchoolResponse,
)
from app.services.content_safety import moderate_text, record_safety_event


router = APIRouter(prefix="/organizations", tags=["学校与班级"])
ROLE_RANK = {"student": 1, "teacher": 2, "school_admin": 3}


def _enabled() -> None:
    if not settings.organizations_enabled:
        raise HTTPException(status_code=404, detail="学校与班级功能尚未开放")


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _invite_hash(code: str) -> str:
    return hmac.new(settings.jwt_secret.encode(), f"invite:{code.strip()}".encode(), hashlib.sha256).hexdigest()


def _anonymous_student_id(class_id: str, user_id: str) -> str:
    return hmac.new(
        settings.jwt_secret.encode(),
        f"class:{class_id}:student:{user_id}".encode(),
        hashlib.sha256,
    ).hexdigest()[:20]


def _school_membership(db: DbSession, user: User, school_id: str) -> SchoolMembership | None:
    return db.scalar(
        select(SchoolMembership).where(
            SchoolMembership.school_id == school_id,
            SchoolMembership.user_id == user.id,
            SchoolMembership.status == "active",
        )
    )


def _require_school_role(
    db: DbSession,
    user: User,
    school_id: str,
    roles: set[str],
) -> SchoolMembership | None:
    if user.role == UserRole.admin:
        return None
    membership = _school_membership(db, user, school_id)
    if membership is None or membership.role not in roles:
        raise HTTPException(status_code=403, detail="没有该学校的操作权限")
    return membership


def _classroom(db: DbSession, class_id: str) -> SchoolClass:
    classroom = db.get(SchoolClass, class_id)
    if classroom is None or classroom.status != "active":
        raise HTTPException(status_code=404, detail="班级不存在")
    return classroom


def _pilot_approval_is_valid(approval: PilotEnrollmentApproval | None) -> bool:
    return bool(
        approval
        and approval.status == "approved"
        and approval.school_authorization_confirmed
        and approval.voluntary_participation_confirmed
        and (
            not approval.guardian_authorization_required
            or approval.guardian_authorization_confirmed
        )
        and approval.revoked_at is None
    )


def _pilot_approval_response(
    approval: PilotEnrollmentApproval,
    username: str,
) -> PilotEnrollmentApprovalResponse:
    return PilotEnrollmentApprovalResponse.model_validate(
        {
            "id": approval.id,
            "school_id": approval.school_id,
            "user_id": approval.user_id,
            "username": username,
            "status": approval.status,
            "school_authorization_confirmed": approval.school_authorization_confirmed,
            "voluntary_participation_confirmed": approval.voluntary_participation_confirmed,
            "guardian_authorization_required": approval.guardian_authorization_required,
            "guardian_authorization_confirmed": approval.guardian_authorization_confirmed,
            "approval_basis": approval.approval_basis,
            "approved_by": approval.approved_by,
            "approved_at": approval.approved_at,
            "revoked_at": approval.revoked_at,
            "created_at": approval.created_at,
            "updated_at": approval.updated_at,
        }
    )


@router.post("/schools", response_model=SchoolResponse, status_code=status.HTTP_201_CREATED)
def create_school(payload: SchoolCreate, db: DbSession, admin: SuperAdminUser) -> School:
    _enabled()
    code = payload.code.lower()
    if db.scalar(select(School.id).where(School.code == code)):
        raise HTTPException(status_code=409, detail="学校代码已存在")
    school = School(name=payload.name.strip(), code=code, created_by=admin.id)
    db.add(school)
    db.flush()
    db.add(SchoolMembership(school_id=school.id, user_id=admin.id, role="school_admin"))
    db.add(AuditLog(actor_user_id=admin.id, action="school.created", target_type="school", target_id=school.id))
    db.commit()
    db.refresh(school)
    return school


@router.get("/me", response_model=OrganizationMeResponse)
def my_organizations(db: DbSession, user: CurrentUser) -> OrganizationMeResponse:
    _enabled()
    memberships = list(
        db.scalars(
            select(SchoolMembership)
            .where(SchoolMembership.user_id == user.id, SchoolMembership.status == "active")
            .order_by(SchoolMembership.joined_at)
        )
    )
    schools = {school.id: school for school in db.scalars(select(School).where(School.id.in_([m.school_id for m in memberships])))} if memberships else {}
    return OrganizationMeResponse(
        memberships=[
            SchoolMembershipResponse.model_validate(
                {
                    "id": membership.id,
                    "school_id": membership.school_id,
                    "role": membership.role,
                    "status": membership.status,
                    "joined_at": membership.joined_at,
                    "school": SchoolResponse.model_validate(schools[membership.school_id]),
                }
            )
            for membership in memberships
        ]
    )


@router.delete("/schools/{school_id}/membership", status_code=status.HTTP_204_NO_CONTENT)
def leave_school(school_id: str, db: DbSession, user: CurrentUser) -> Response:
    _enabled()
    membership = _school_membership(db, user, school_id)
    if membership is None:
        raise HTTPException(status_code=404, detail="不属于该学校")
    membership.status = "left"
    db.execute(
        update(ClassMembership)
        .where(
            ClassMembership.user_id == user.id,
            ClassMembership.class_id.in_(
                select(SchoolClass.id).where(SchoolClass.school_id == school_id)
            ),
        )
        .values(status="left")
    )
    if user.tenant_id == school_id:
        user.tenant_id = None
    db.add(
        AuditLog(
            actor_user_id=user.id,
            action="school.membership_left",
            target_type="school",
            target_id=school_id,
        )
    )
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/schools/{school_id}/classes", response_model=list[SchoolClassResponse])
def list_classes(school_id: str, db: DbSession, user: CurrentUser) -> list[SchoolClass]:
    _enabled()
    membership = _require_school_role(db, user, school_id, {"school_admin", "teacher", "student"})
    query = select(SchoolClass).where(
        SchoolClass.school_id == school_id,
        SchoolClass.status == "active",
    )
    if user.role != UserRole.admin and membership is not None and membership.role != "school_admin":
        query = query.join(ClassMembership, ClassMembership.class_id == SchoolClass.id).where(
            ClassMembership.user_id == user.id,
            ClassMembership.status == "active",
        )
    return list(db.scalars(query.order_by(SchoolClass.academic_year.desc(), SchoolClass.name)))


@router.post("/schools/{school_id}/classes", response_model=SchoolClassResponse, status_code=201)
def create_class(
    school_id: str,
    payload: SchoolClassCreate,
    db: DbSession,
    user: CurrentUser,
) -> SchoolClass:
    _enabled()
    school = db.get(School, school_id)
    if school is None or school.status != "active":
        raise HTTPException(status_code=404, detail="学校不存在")
    _require_school_role(db, user, school_id, {"school_admin"})
    duplicate = db.scalar(
        select(SchoolClass.id).where(
            SchoolClass.school_id == school_id,
            SchoolClass.name == payload.name.strip(),
            SchoolClass.academic_year == payload.academic_year,
        )
    )
    if duplicate:
        raise HTTPException(status_code=409, detail="同学年班级名称已存在")
    classroom = SchoolClass(
        school_id=school_id,
        name=payload.name.strip(),
        grade=payload.grade,
        academic_year=payload.academic_year,
        created_by=user.id,
    )
    db.add(classroom)
    db.flush()
    db.add(ClassMembership(class_id=classroom.id, user_id=user.id, role="teacher"))
    db.add(AuditLog(actor_user_id=user.id, action="class.created", target_type="class", target_id=classroom.id))
    db.commit()
    db.refresh(classroom)
    return classroom


@router.get(
    "/schools/{school_id}/pilot-approvals",
    response_model=list[PilotEnrollmentApprovalResponse],
)
def list_pilot_approvals(
    school_id: str,
    db: DbSession,
    user: CurrentUser,
) -> list[PilotEnrollmentApprovalResponse]:
    _enabled()
    school = db.get(School, school_id)
    if school is None or school.status != "active":
        raise HTTPException(status_code=404, detail="学校不存在")
    _require_school_role(db, user, school_id, {"school_admin"})
    rows = db.execute(
        select(PilotEnrollmentApproval, User.username)
        .join(User, User.id == PilotEnrollmentApproval.user_id)
        .where(PilotEnrollmentApproval.school_id == school_id)
        .order_by(PilotEnrollmentApproval.updated_at.desc())
    ).all()
    return [_pilot_approval_response(approval, username) for approval, username in rows]


@router.post(
    "/schools/{school_id}/pilot-approvals",
    response_model=PilotEnrollmentApprovalResponse,
)
def approve_pilot_enrollment(
    school_id: str,
    payload: PilotEnrollmentApprovalCreate,
    db: DbSession,
    user: CurrentUser,
) -> PilotEnrollmentApprovalResponse:
    _enabled()
    school = db.get(School, school_id)
    if school is None or school.status != "active":
        raise HTTPException(status_code=404, detail="学校不存在")
    _require_school_role(db, user, school_id, {"school_admin"})
    participant = db.get(User, payload.user_id)
    if participant is None or not participant.is_active:
        raise HTTPException(status_code=404, detail="参与者账户不存在或不可用")
    if not payload.school_authorization_confirmed or not payload.voluntary_participation_confirmed:
        raise HTTPException(status_code=422, detail="必须确认学校授权和参与者自愿参与")
    if payload.guardian_authorization_required and not payload.guardian_authorization_confirmed:
        raise HTTPException(status_code=422, detail="该参与者需要监护人授权确认")
    safety = moderate_text(payload.approval_basis)
    if not safety.allowed:
        record_safety_event(
            db,
            user_id=user.id,
            action="pilot.enrollment_approval_blocked",
            decision=safety,
            content=payload.approval_basis,
            target_type="school",
            target_id=school_id,
        )
        db.commit()
        raise HTTPException(status_code=422, detail="授权依据不能包含个人身份或联系方式")

    now = datetime.now(timezone.utc)
    approval = db.scalar(
        select(PilotEnrollmentApproval).where(
            PilotEnrollmentApproval.school_id == school_id,
            PilotEnrollmentApproval.user_id == participant.id,
        )
    )
    if approval is None:
        approval = PilotEnrollmentApproval(
            school_id=school_id,
            user_id=participant.id,
            approval_basis=payload.approval_basis.strip(),
        )
        db.add(approval)
    approval.status = "approved"
    approval.school_authorization_confirmed = payload.school_authorization_confirmed
    approval.voluntary_participation_confirmed = payload.voluntary_participation_confirmed
    approval.guardian_authorization_required = payload.guardian_authorization_required
    approval.guardian_authorization_confirmed = payload.guardian_authorization_confirmed
    approval.approval_basis = payload.approval_basis.strip()
    approval.approved_by = user.id
    approval.approved_at = now
    approval.revoked_at = None
    db.flush()
    db.add(
        AuditLog(
            actor_user_id=user.id,
            action="pilot.enrollment_approved",
            target_type="pilot_enrollment_approval",
            target_id=approval.id,
            details={
                "school_id": school_id,
                "user_id": participant.id,
                "school_authorized": True,
                "voluntary": True,
                "guardian_required": approval.guardian_authorization_required,
                "guardian_confirmed": approval.guardian_authorization_confirmed,
                "approval_basis_recorded": bool(approval.approval_basis),
            },
        )
    )
    db.commit()
    db.refresh(approval)
    return _pilot_approval_response(approval, participant.username)


@router.delete(
    "/schools/{school_id}/pilot-approvals/{participant_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def revoke_pilot_enrollment(
    school_id: str,
    participant_id: str,
    db: DbSession,
    user: CurrentUser,
) -> Response:
    _enabled()
    school = db.get(School, school_id)
    if school is None or school.status != "active":
        raise HTTPException(status_code=404, detail="学校不存在")
    _require_school_role(db, user, school_id, {"school_admin"})
    approval = db.scalar(
        select(PilotEnrollmentApproval).where(
            PilotEnrollmentApproval.school_id == school_id,
            PilotEnrollmentApproval.user_id == participant_id,
        )
    )
    if approval is None:
        raise HTTPException(status_code=404, detail="试点准入记录不存在")
    approval.status = "revoked"
    approval.revoked_at = datetime.now(timezone.utc)
    membership = db.scalar(
        select(SchoolMembership).where(
            SchoolMembership.school_id == school_id,
            SchoolMembership.user_id == participant_id,
        )
    )
    if membership is not None:
        membership.status = "left"
    db.execute(
        update(ClassMembership)
        .where(
            ClassMembership.user_id == participant_id,
            ClassMembership.class_id.in_(
                select(SchoolClass.id).where(SchoolClass.school_id == school_id)
            ),
        )
        .values(status="left")
    )
    participant = db.get(User, participant_id)
    if participant is not None and participant.tenant_id == school_id:
        participant.tenant_id = None
    db.add(
        AuditLog(
            actor_user_id=user.id,
            action="pilot.enrollment_revoked",
            target_type="pilot_enrollment_approval",
            target_id=approval.id,
            details={"school_id": school_id, "user_id": participant_id},
        )
    )
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/schools/{school_id}/invites", response_model=OrganizationInviteResponse, status_code=201)
def create_invite(
    school_id: str,
    payload: OrganizationInviteCreate,
    db: DbSession,
    user: CurrentUser,
) -> dict:
    _enabled()
    school = db.get(School, school_id)
    if school is None or school.status != "active":
        raise HTTPException(status_code=404, detail="学校不存在")
    membership = _require_school_role(db, user, school_id, {"school_admin", "teacher"})
    classroom = None
    if payload.class_id:
        classroom = _classroom(db, payload.class_id)
        if classroom.school_id != school_id:
            raise HTTPException(status_code=409, detail="班级不属于该学校")
    actor_is_teacher = user.role != UserRole.admin and membership is not None and membership.role == "teacher"
    if actor_is_teacher:
        if payload.member_role != "student" or classroom is None:
            raise HTTPException(status_code=403, detail="教师只能为自己班级创建学生邀请码")
        class_membership = db.scalar(
            select(ClassMembership.id).where(
                ClassMembership.class_id == classroom.id,
                ClassMembership.user_id == user.id,
                ClassMembership.role == "teacher",
                ClassMembership.status == "active",
            )
        )
        if class_membership is None:
            raise HTTPException(status_code=403, detail="教师不属于该班级")
    code = f"QK-{secrets.token_urlsafe(12)}"
    invite = OrganizationInvite(
        school_id=school_id,
        class_id=classroom.id if classroom else None,
        code_hash=_invite_hash(code),
        member_role=payload.member_role,
        max_uses=payload.max_uses,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=payload.expires_hours),
        created_by=user.id,
    )
    db.add(invite)
    db.flush()
    db.add(AuditLog(actor_user_id=user.id, action="invite.created", target_type="invite", target_id=invite.id, details={"role": invite.member_role, "class_id": invite.class_id, "max_uses": invite.max_uses}))
    db.commit()
    return {
        "id": invite.id,
        "school_id": invite.school_id,
        "class_id": invite.class_id,
        "member_role": invite.member_role,
        "max_uses": invite.max_uses,
        "use_count": invite.use_count,
        "expires_at": invite.expires_at,
        "is_active": invite.is_active,
        "code": code,
    }


@router.post("/invites/redeem", response_model=OrganizationJoinResponse)
def redeem_invite(
    payload: OrganizationInviteRedeem,
    db: DbSession,
    user: CurrentUser,
) -> OrganizationJoinResponse:
    _enabled()
    invite = db.scalar(
        select(OrganizationInvite)
        .where(OrganizationInvite.code_hash == _invite_hash(payload.code))
        .with_for_update()
    )
    now = datetime.now(timezone.utc)
    if (
        invite is None
        or not invite.is_active
        or _as_utc(invite.expires_at) <= now
        or invite.use_count >= invite.max_uses
    ):
        raise HTTPException(status_code=404, detail="邀请码无效或已过期")
    school = db.get(School, invite.school_id)
    if school is None or school.status != "active":
        raise HTTPException(status_code=404, detail="学校不可用")
    if invite.member_role == "student" and settings.pilot_authorization_enforced:
        approval = db.scalar(
            select(PilotEnrollmentApproval).where(
                PilotEnrollmentApproval.school_id == school.id,
                PilotEnrollmentApproval.user_id == user.id,
            )
        )
        if not _pilot_approval_is_valid(approval):
            raise HTTPException(status_code=403, detail="尚未完成学校试点准入授权")
    if user.tenant_id is not None and user.tenant_id != school.id:
        raise HTTPException(status_code=409, detail="账户已属于其他学校")
    classroom = _classroom(db, invite.class_id) if invite.class_id else None
    membership = db.scalar(
        select(SchoolMembership).where(
            SchoolMembership.school_id == school.id,
            SchoolMembership.user_id == user.id,
        )
    )
    joined = False
    if membership is None:
        membership = SchoolMembership(school_id=school.id, user_id=user.id, role=invite.member_role)
        db.add(membership)
        joined = True
    else:
        role_promoted = ROLE_RANK[invite.member_role] > ROLE_RANK[membership.role]
        if role_promoted:
            membership.role = invite.member_role
        if membership.status != "active" or role_promoted:
            membership.status = "active"
            joined = True
    if classroom is not None:
        class_membership = db.scalar(
            select(ClassMembership).where(
                ClassMembership.class_id == classroom.id,
                ClassMembership.user_id == user.id,
            )
        )
        if class_membership is None:
            db.add(ClassMembership(class_id=classroom.id, user_id=user.id, role=invite.member_role))
            joined = True
        else:
            role_promoted = ROLE_RANK[invite.member_role] > ROLE_RANK[class_membership.role]
            if role_promoted:
                class_membership.role = invite.member_role
            if class_membership.status != "active" or role_promoted:
                class_membership.status = "active"
                joined = True
    if joined:
        invite.use_count += 1
        user.tenant_id = school.id
        db.add(AuditLog(actor_user_id=user.id, action="invite.redeemed", target_type="invite", target_id=invite.id, details={"school_id": school.id, "class_id": invite.class_id, "role": invite.member_role}))
    db.commit()
    return OrganizationJoinResponse(
        school=SchoolResponse.model_validate(school),
        school_role=membership.role,
        classroom=SchoolClassResponse.model_validate(classroom) if classroom else None,
        joined=joined,
    )


@router.get("/classes/{class_id}/overview", response_model=ClassOverviewResponse)
def class_overview(class_id: str, db: DbSession, user: CurrentUser) -> ClassOverviewResponse:
    _enabled()
    classroom = _classroom(db, class_id)
    school_membership = _require_school_role(db, user, classroom.school_id, {"school_admin", "teacher"})
    if user.role != UserRole.admin and school_membership is not None and school_membership.role == "teacher":
        is_teacher = db.scalar(
            select(ClassMembership.id).where(
                ClassMembership.class_id == class_id,
                ClassMembership.user_id == user.id,
                ClassMembership.role == "teacher",
                ClassMembership.status == "active",
            )
        )
        if is_teacher is None:
            raise HTTPException(status_code=403, detail="教师不属于该班级")
    student_memberships = list(
        db.scalars(
            select(ClassMembership).where(
                ClassMembership.class_id == class_id,
                ClassMembership.role == "student",
                ClassMembership.status == "active",
            )
        )
    )
    user_ids = [membership.user_id for membership in student_memberships]
    questions: dict[str, int] = {}
    mistakes: dict[str, int] = {}
    verified: dict[str, int] = {}
    last_activity: dict[str, datetime] = {}
    if user_ids:
        questions = dict(
            db.execute(
                select(Conversation.user_id, func.count(Message.id))
                .join(Message, Message.conversation_id == Conversation.id)
                .where(Conversation.user_id.in_(user_ids), Message.role == "student")
                .group_by(Conversation.user_id)
            ).all()
        )
        mistakes = dict(
            db.execute(
                select(MistakeProblem.user_id, func.count(MistakeProblem.id))
                .where(MistakeProblem.user_id.in_(user_ids))
                .group_by(MistakeProblem.user_id)
            ).all()
        )
        verified = dict(
            db.execute(
                select(UserKnowledgeState.user_id, func.count(UserKnowledgeState.node_id))
                .where(
                    UserKnowledgeState.user_id.in_(user_ids),
                    UserKnowledgeState.status == KnowledgeStatus.verified,
                )
                .group_by(UserKnowledgeState.user_id)
            ).all()
        )
        last_activity = dict(
            db.execute(
                select(LearningEvent.user_id, func.max(LearningEvent.created_at))
                .where(LearningEvent.user_id.in_(user_ids))
                .group_by(LearningEvent.user_id)
            ).all()
        )
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    top_error_categories: list[dict] = []
    weak_knowledge_points: list[dict] = []
    practice_completion_rate = 0.0
    second_attempt_accuracy = 0.0
    due_review_count = 0
    if user_ids:
        category_rows = db.execute(
            select(MistakeProblem.error_category, func.count(MistakeProblem.id))
            .where(
                MistakeProblem.user_id.in_(user_ids),
                MistakeProblem.error_category.is_not(None),
            )
            .group_by(MistakeProblem.error_category)
            .order_by(func.count(MistakeProblem.id).desc())
            .limit(5)
        ).all()
        top_error_categories = [
            {"label": category, "count": count} for category, count in category_rows
        ]
        weak_rows = db.execute(
            select(KnowledgeNode.name, func.count(MistakeProblem.id))
            .join(MistakeProblem, MistakeProblem.knowledge_node_id == KnowledgeNode.id)
            .where(
                MistakeProblem.user_id.in_(user_ids),
                MistakeProblem.study_status != "mastered",
            )
            .group_by(KnowledgeNode.name)
            .order_by(func.count(MistakeProblem.id).desc())
            .limit(5)
        ).all()
        weak_knowledge_points = [
            {"label": name, "count": count} for name, count in weak_rows
        ]
        recent_rounds = list(
            db.scalars(
                select(MistakePracticeRound).where(
                    MistakePracticeRound.user_id.in_(user_ids),
                    MistakePracticeRound.started_at >= cutoff,
                )
            )
        )
        if recent_rounds:
            practice_completion_rate = round(
                sum(item.status == "completed" for item in recent_rounds) / len(recent_rounds),
                4,
            )
        second_attempts = list(
            db.scalars(
                select(MistakeProblem.second_attempt_correct).where(
                    MistakeProblem.user_id.in_(user_ids),
                    MistakeProblem.second_attempt_correct.is_not(None),
                    MistakeProblem.last_reviewed_at >= cutoff,
                )
            )
        )
        if second_attempts:
            second_attempt_accuracy = round(
                sum(value is True for value in second_attempts) / len(second_attempts),
                4,
            )
        due_review_count = db.scalar(
            select(func.count(MistakeProblem.id)).where(
                MistakeProblem.user_id.in_(user_ids),
                MistakeProblem.study_status != "mastered",
                MistakeProblem.next_review_at.is_not(None),
                MistakeProblem.next_review_at <= datetime.now(timezone.utc),
            )
        ) or 0
    students = [
        ClassStudentOverview(
            anonymous_id=_anonymous_student_id(class_id, membership.user_id),
            joined_at=membership.joined_at,
            last_activity_at=last_activity.get(membership.user_id),
            questions=questions.get(membership.user_id, 0),
            mistakes=mistakes.get(membership.user_id, 0),
            verified_nodes=verified.get(membership.user_id, 0),
        )
        for membership in student_memberships
    ]
    return ClassOverviewResponse(
        classroom=SchoolClassResponse.model_validate(classroom),
        student_count=len(students),
        active_7d_students=sum(
            1 for item in students if item.last_activity_at and _as_utc(item.last_activity_at) >= cutoff
        ),
        questions=sum(item.questions for item in students),
        mistakes=sum(item.mistakes for item in students),
        verified_nodes=sum(item.verified_nodes for item in students),
        top_error_categories=top_error_categories,
        weak_knowledge_points=weak_knowledge_points,
        practice_completion_rate=practice_completion_rate,
        second_attempt_accuracy=second_attempt_accuracy,
        due_review_count=due_review_count,
        students=students,
    )
