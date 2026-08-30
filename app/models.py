from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def new_id() -> str:
    return str(uuid.uuid4())


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class UserRole(str, enum.Enum):
    student = "student"
    content_admin = "content_admin"
    admin = "admin"


class KnowledgeStatus(str, enum.Enum):
    unexplored = "unexplored"
    explored = "explored"
    understood = "understood"
    verified = "verified"
    unstable = "unstable"
    error_prone = "error_prone"
    to_explore = "to_explore"


class EdgeType(str, enum.Enum):
    prerequisite = "prerequisite"
    related = "related"
    confused_with = "confused_with"
    question_type = "question_type"
    extension = "extension"


class QaMode(str, enum.Enum):
    knowledge = "knowledge"
    problem = "problem"
    error = "error"
    review = "review"
    explore = "explore"
    verify = "verify"


class HelpLevel(str, enum.Enum):
    keyword = "keyword"
    next_step = "next_step"
    approach = "approach"
    full = "full"
    conclusion = "conclusion"


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    username: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    nickname: Mapped[str] = mapped_column(String(40))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[UserRole] = mapped_column(Enum(UserRole), default=UserRole.student)
    tenant_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    credit_account: Mapped[CreditAccount] = relationship(back_populates="user", uselist=False)


class RefreshSession(Base):
    __tablename__ = "refresh_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_jti: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    device_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class School(Base):
    __tablename__ = "schools"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(120))
    code: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    settings: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class SchoolMembership(Base):
    __tablename__ = "school_memberships"
    __table_args__ = (UniqueConstraint("school_id", "user_id", name="uq_school_membership"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    school_id: Mapped[str] = mapped_column(ForeignKey("schools.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(20), index=True)
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class SchoolClass(Base):
    __tablename__ = "school_classes"
    __table_args__ = (UniqueConstraint("school_id", "name", "academic_year", name="uq_school_class_name_year"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    school_id: Mapped[str] = mapped_column(ForeignKey("schools.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    grade: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    academic_year: Mapped[str] = mapped_column(String(20), index=True)
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    created_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ClassMembership(Base):
    __tablename__ = "class_memberships"
    __table_args__ = (UniqueConstraint("class_id", "user_id", name="uq_class_membership"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    class_id: Mapped[str] = mapped_column(ForeignKey("school_classes.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(20), index=True)
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class OrganizationInvite(Base):
    __tablename__ = "organization_invites"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    school_id: Mapped[str] = mapped_column(ForeignKey("schools.id", ondelete="CASCADE"), index=True)
    class_id: Mapped[str | None] = mapped_column(
        ForeignKey("school_classes.id", ondelete="CASCADE"), nullable=True, index=True
    )
    code_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    member_role: Mapped[str] = mapped_column(String(20), index=True)
    max_uses: Mapped[int] = mapped_column(Integer, default=1)
    use_count: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class KnowledgeSource(Base):
    __tablename__ = "knowledge_sources"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(255))
    publisher: Mapped[str | None] = mapped_column(String(120), nullable=True)
    edition: Mapped[str | None] = mapped_column(String(80), nullable=True)
    location: Mapped[str] = mapped_column(String(255))
    authorization_status: Mapped[str] = mapped_column(String(40), default="internal_demo")


class KnowledgeDocument(Base):
    __tablename__ = "knowledge_documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String(255), index=True)
    subject: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    grade: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    textbook_version: Mapped[str | None] = mapped_column(String(80), nullable=True)
    chapter: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    document_role: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    source_type: Mapped[str] = mapped_column(String(40), default="local_file")
    source_uri: Mapped[str] = mapped_column(String(1000))
    authorization_status: Mapped[str] = mapped_column(String(40), index=True)
    checksum_sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    mime_type: Mapped[str | None] = mapped_column(String(120), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    document_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    chunks: Mapped[list[KnowledgeChunk]] = relationship(
        back_populates="document",
        cascade="all, delete-orphan",
        order_by="KnowledgeChunk.sequence",
    )


class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunks"
    __table_args__ = (UniqueConstraint("document_id", "sequence"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_documents.id", ondelete="CASCADE"),
        index=True,
    )
    sequence: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    content_type: Mapped[str] = mapped_column(String(20), default="text", index=True)
    formula_latex: Mapped[str | None] = mapped_column(Text, nullable=True)
    formula_source: Mapped[str | None] = mapped_column(String(30), nullable=True)
    ocr_confidence: Mapped[float | None] = mapped_column(nullable=True)
    formula_review_status: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    formula_review_note: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    char_start: Mapped[int] = mapped_column(Integer)
    char_end: Mapped[int] = mapped_column(Integer)
    embedding: Mapped[list[float] | None] = mapped_column(JSON, nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    document: Mapped[KnowledgeDocument] = relationship(back_populates="chunks")


class KnowledgeNode(Base):
    __tablename__ = "knowledge_nodes"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), index=True)
    subject: Mapped[str] = mapped_column(String(40), index=True)
    grade: Mapped[str] = mapped_column(String(40), index=True)
    textbook_version: Mapped[str] = mapped_column(String(80))
    chapter: Mapped[str] = mapped_column(String(120), index=True)
    definition: Mapped[str] = mapped_column(Text)
    explanation: Mapped[str] = mapped_column(Text)
    common_errors: Mapped[list[str]] = mapped_column(JSON, default=list)
    question_types: Mapped[list[str]] = mapped_column(JSON, default=list)
    source_id: Mapped[str] = mapped_column(ForeignKey("knowledge_sources.id"))
    source_excerpt: Mapped[str] = mapped_column(String(255))
    version: Mapped[int] = mapped_column(Integer, default=1)
    review_status: Mapped[str] = mapped_column(String(30), default="approved", index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    source: Mapped[KnowledgeSource] = relationship()
    versions: Mapped[list[KnowledgeNodeVersion]] = relationship(
        back_populates="node", cascade="all, delete-orphan", order_by="KnowledgeNodeVersion.version"
    )


class KnowledgeNodeVersion(Base):
    __tablename__ = "knowledge_node_versions"
    __table_args__ = (UniqueConstraint("node_id", "version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    node_id: Mapped[str] = mapped_column(ForeignKey("knowledge_nodes.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    snapshot: Mapped[dict] = mapped_column(JSON)
    change_note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="draft", index=True)
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    withdrawn_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    node: Mapped[KnowledgeNode] = relationship(back_populates="versions")


class KnowledgeEdge(Base):
    __tablename__ = "knowledge_edges"
    __table_args__ = (UniqueConstraint("source_node_id", "target_node_id", "edge_type"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source_node_id: Mapped[str] = mapped_column(ForeignKey("knowledge_nodes.id", ondelete="CASCADE"), index=True)
    target_node_id: Mapped[str] = mapped_column(ForeignKey("knowledge_nodes.id", ondelete="CASCADE"), index=True)
    edge_type: Mapped[EdgeType] = mapped_column(Enum(EdgeType), index=True)
    explanation: Mapped[str] = mapped_column(String(255))


class UserKnowledgeState(Base):
    __tablename__ = "user_knowledge_states"
    __table_args__ = (UniqueConstraint("user_id", "node_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    node_id: Mapped[str] = mapped_column(ForeignKey("knowledge_nodes.id", ondelete="CASCADE"), index=True)
    status: Mapped[KnowledgeStatus] = mapped_column(Enum(KnowledgeStatus), default=KnowledgeStatus.unexplored)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_favorite: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(120), default="新对话")
    mode: Mapped[QaMode] = mapped_column(Enum(QaMode))
    subject: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    knowledge_node_id: Mapped[str | None] = mapped_column(ForeignKey("knowledge_nodes.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    messages: Mapped[list[Message]] = relationship(back_populates="conversation", cascade="all, delete-orphan", order_by="Message.created_at")


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    structured_content: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    citations: Mapped[list[dict]] = mapped_column(JSON, default=list)
    linked_node_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    provider: Mapped[str | None] = mapped_column(String(40), nullable=True)
    model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(30), nullable=True)
    knowledge_version: Mapped[str | None] = mapped_column(String(30), nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class IdempotencyRequest(Base):
    __tablename__ = "idempotency_requests"
    __table_args__ = (
        UniqueConstraint("user_id", "scope", "key", name="uq_idempotency_user_scope_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    scope: Mapped[str] = mapped_column(String(120))
    key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="processing", index=True)
    response_body: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class ModelCall(Base):
    __tablename__ = "model_calls"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    feature: Mapped[str] = mapped_column(String(40), index=True)
    provider: Mapped[str] = mapped_column(String(40), index=True)
    model: Mapped[str] = mapped_column(String(80), index=True)
    success: Mapped[bool] = mapped_column(Boolean, index=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    reference_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


class LearningEvent(Base):
    __tablename__ = "learning_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    node_id: Mapped[str | None] = mapped_column(ForeignKey("knowledge_nodes.id"), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(40), index=True)
    event_data: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


class LearningCheckAttempt(Base):
    __tablename__ = "learning_check_attempts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    node_id: Mapped[str] = mapped_column(ForeignKey("knowledge_nodes.id", ondelete="CASCADE"), index=True)
    prompt: Mapped[str] = mapped_column(String(500))
    choices: Mapped[list[dict]] = mapped_column(JSON)
    correct_choice_id: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


class CreditAccount(Base):
    __tablename__ = "credit_accounts"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    balance: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    user: Mapped[User] = relationship(back_populates="credit_account")


class CreditLedger(Base):
    __tablename__ = "credit_ledger"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    amount: Mapped[int] = mapped_column(Integer)
    balance_after: Mapped[int] = mapped_column(Integer)
    entry_type: Mapped[str] = mapped_column(String(40), index=True)
    feature: Mapped[str] = mapped_column(String(40))
    reference_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(40), nullable=True)
    model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


class CreditCampaign(Base):
    __tablename__ = "credit_campaigns"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(120))
    amount: Mapped[int] = mapped_column(Integer)
    school_id: Mapped[str | None] = mapped_column(
        ForeignKey("schools.id", ondelete="CASCADE"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    max_redemptions: Mapped[int] = mapped_column(Integer)
    redemption_count: Mapped[int] = mapped_column(Integer, default=0)
    per_user_limit: Mapped[int] = mapped_column(Integer, default=1)
    created_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class CreditCode(Base):
    __tablename__ = "credit_codes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    campaign_id: Mapped[str] = mapped_column(
        ForeignKey("credit_campaigns.id", ondelete="CASCADE"), index=True
    )
    code_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    max_uses: Mapped[int] = mapped_column(Integer, default=1)
    use_count: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class CreditRedemption(Base):
    __tablename__ = "credit_redemptions"
    __table_args__ = (UniqueConstraint("code_id", "user_id", name="uq_credit_code_user"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    campaign_id: Mapped[str] = mapped_column(
        ForeignKey("credit_campaigns.id", ondelete="CASCADE"), index=True
    )
    code_id: Mapped[str] = mapped_column(ForeignKey("credit_codes.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    amount: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


class ContentContribution(Base):
    __tablename__ = "content_contributions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    school_id: Mapped[str | None] = mapped_column(
        ForeignKey("schools.id", ondelete="SET NULL"), nullable=True, index=True
    )
    contribution_type: Mapped[str] = mapped_column(String(30), index=True)
    title: Mapped[str] = mapped_column(String(160))
    content: Mapped[str] = mapped_column(Text)
    source_reference: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="queued", index=True)
    ai_review: Mapped[dict] = mapped_column(JSON, default=dict)
    ai_provider: Mapped[str | None] = mapped_column(String(40), nullable=True)
    ai_model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    review_note: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reward_amount: Mapped[int] = mapped_column(Integer, default=0)
    reward_status: Mapped[str] = mapped_column(String(20), default="disabled", index=True)
    reward_available_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class FeedbackSubmission(Base):
    __tablename__ = "feedback_submissions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    node_id: Mapped[str | None] = mapped_column(ForeignKey("knowledge_nodes.id"), nullable=True)
    message_id: Mapped[str | None] = mapped_column(ForeignKey("messages.id"), nullable=True)
    category: Mapped[str] = mapped_column(String(40))
    content: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    review_note: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    reviewed_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class MistakeProblem(Base):
    __tablename__ = "mistake_problems"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    subject: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    question_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    corrected_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    student_work: Mapped[str | None] = mapped_column(Text, nullable=True)
    question_goal: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    error_category: Mapped[str | None] = mapped_column(String(30), nullable=True, index=True)
    error_note: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    analysis: Mapped[dict] = mapped_column(JSON, default=dict)
    analysis_status: Mapped[str] = mapped_column(String(30), default="not_started", index=True)
    analysis_provider: Mapped[str | None] = mapped_column(String(40), nullable=True)
    analysis_model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    analyzed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    knowledge_node_id: Mapped[str | None] = mapped_column(
        ForeignKey("knowledge_nodes.id", ondelete="SET NULL"), nullable=True, index=True
    )
    link_status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    review_status: Mapped[str] = mapped_column(String(30), default="draft", index=True)
    study_status: Mapped[str] = mapped_column(String(30), default="active", index=True)
    next_review_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    assets: Mapped[list[MistakeAsset]] = relationship(
        back_populates="mistake", cascade="all, delete-orphan", order_by="MistakeAsset.created_at"
    )
    ocr_tasks: Mapped[list[OcrTask]] = relationship(
        back_populates="mistake", cascade="all, delete-orphan", order_by="OcrTask.created_at"
    )
    practices: Mapped[list[MistakePractice]] = relationship(
        back_populates="mistake", cascade="all, delete-orphan", order_by="MistakePractice.created_at"
    )


class MistakeAsset(Base):
    __tablename__ = "mistake_assets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    mistake_id: Mapped[str] = mapped_column(ForeignKey("mistake_problems.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    object_key: Mapped[str] = mapped_column(String(1000), unique=True)
    mime_type: Mapped[str] = mapped_column(String(80))
    size_bytes: Mapped[int] = mapped_column(Integer)
    checksum_sha256: Mapped[str] = mapped_column(String(64), index=True)
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30), default="uploading", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    mistake: Mapped[MistakeProblem] = relationship(back_populates="assets")


class OcrTask(Base):
    __tablename__ = "ocr_tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    mistake_id: Mapped[str] = mapped_column(ForeignKey("mistake_problems.id", ondelete="CASCADE"), index=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("mistake_assets.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(30), default="uploading", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    result_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    formulas: Mapped[list[dict]] = mapped_column(JSON, default=list)
    confidence: Mapped[float | None] = mapped_column(nullable=True)
    requires_review: Mapped[bool] = mapped_column(Boolean, default=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)

    mistake: Mapped[MistakeProblem] = relationship(back_populates="ocr_tasks")


class MistakePractice(Base):
    __tablename__ = "mistake_practices"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    mistake_id: Mapped[str] = mapped_column(ForeignKey("mistake_problems.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    question_text: Mapped[str] = mapped_column(Text)
    answer_reference: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(30), default="generated")
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    student_answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_correct: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    validation_details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    mistake: Mapped[MistakeProblem] = relationship(back_populates="practices")


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    actor_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(80), index=True)
    target_type: Mapped[str] = mapped_column(String(60))
    target_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


Index("ix_edges_source_type", KnowledgeEdge.source_node_id, KnowledgeEdge.edge_type)
Index("ix_events_user_created", LearningEvent.user_id, LearningEvent.created_at)
