from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models import EdgeType, HelpLevel, KnowledgeStatus, QaMode, UserRole


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class MessageResponse(ApiModel):
    message: str


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32, pattern=r"^[A-Za-z0-9_]+$")
    password: str = Field(min_length=8, max_length=128)
    email: EmailStr | None = None
    nickname: str | None = Field(default=None, min_length=1, max_length=40)
    device_name: str | None = Field(default=None, max_length=120)


class LoginRequest(BaseModel):
    username: str
    password: str
    device_name: str | None = Field(default=None, max_length=120)


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=128)


class UserResponse(ApiModel):
    id: str
    username: str
    email: str | None
    nickname: str
    role: UserRole
    tenant_id: str | None
    created_at: datetime


class AuthResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserResponse


class SourceResponse(ApiModel):
    id: str
    title: str
    publisher: str | None
    edition: str | None
    location: str
    authorization_status: str


class KnowledgeNodeSummary(ApiModel):
    id: str
    name: str
    subject: str
    grade: str
    chapter: str
    definition: str
    status: KnowledgeStatus = KnowledgeStatus.unexplored


class KnowledgeNodeDetail(KnowledgeNodeSummary):
    textbook_version: str
    explanation: str
    common_errors: list[str]
    question_types: list[str]
    source_excerpt: str
    version: int
    review_status: str
    updated_at: datetime
    source: SourceResponse


class NeighborNode(KnowledgeNodeSummary):
    edge_type: EdgeType
    edge_explanation: str


class NeighborResponse(BaseModel):
    center: KnowledgeNodeSummary
    nodes: list[NeighborNode]


class SubjectClassificationResponse(BaseModel):
    subject: str | None
    confidence: float
    margin: float
    source: str
    scores: dict[str, float]


class KnowledgeStateUpdate(BaseModel):
    status: KnowledgeStatus
    note: str | None = Field(default=None, max_length=4000)
    is_favorite: bool | None = None


class LearningEventCreate(BaseModel):
    event_type: str = Field(pattern=r"^(viewed_node|read_answer|marked_understood|marked_confused|favorited|completed_check|submitted_feedback)$")
    node_id: str | None = None
    event_data: dict = Field(default_factory=dict)


class LearningEventResponse(ApiModel):
    id: str
    node_id: str | None
    event_type: str
    event_data: dict
    created_at: datetime


class LearningSummaryItem(KnowledgeNodeSummary):
    updated_at: datetime
    note: str | None
    is_favorite: bool


class LearningSummary(BaseModel):
    recent: list[LearningSummaryItem]
    review: list[LearningSummaryItem]
    error_prone: list[LearningSummaryItem]


class ConversationCreate(BaseModel):
    mode: QaMode = QaMode.knowledge
    knowledge_node_id: str | None = None
    subject: str | None = Field(default=None, min_length=1, max_length=40)
    title: str | None = Field(default=None, max_length=120)


class ConversationMessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=12000)
    help_level: HelpLevel = HelpLevel.approach


class Citation(BaseModel):
    node_id: str
    node_name: str
    source_title: str
    source_location: str
    excerpt: str


class StructuredAnswer(BaseModel):
    conclusion: str
    explanation: str
    evidence: list[str] = Field(default_factory=list)
    next_step: str
    uncertain: bool = False


class ChatMessageResponse(ApiModel):
    id: str
    role: str
    content: str
    structured_content: dict | None
    citations: list[dict]
    linked_node_ids: list[str]
    provider: str | None
    model: str | None
    created_at: datetime


class ConversationResponse(ApiModel):
    id: str
    title: str
    mode: QaMode
    knowledge_node_id: str | None
    subject: str | None
    created_at: datetime
    updated_at: datetime
    messages: list[ChatMessageResponse] = Field(default_factory=list)


class QaResult(BaseModel):
    conversation_id: str
    user_message: ChatMessageResponse
    assistant_message: ChatMessageResponse
    credits_charged: int
    balance: int
    subject: str | None = None


class CreditAccountResponse(ApiModel):
    balance: int
    updated_at: datetime


class CreditLedgerResponse(ApiModel):
    id: str
    amount: int
    balance_after: int
    entry_type: str
    feature: str
    reference_id: str | None
    provider: str | None
    model: str | None
    created_at: datetime


class FeedbackCreate(BaseModel):
    category: str = Field(pattern=r"^(content_error|relation_error|answer_error|version_outdated|product_issue|other)$")
    content: str = Field(min_length=2, max_length=4000)
    node_id: str | None = None
    message_id: str | None = None


class FeedbackResponse(ApiModel):
    id: str
    category: str
    content: str
    node_id: str | None
    message_id: str | None
    status: str
    created_at: datetime


class AdminFeedbackResponse(FeedbackResponse):
    user_id: str
    review_note: str | None = None
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None


class FeedbackReview(BaseModel):
    status: str = Field(pattern=r"^(pending|accepted|rejected|resolved)$")
    review_note: str | None = Field(default=None, max_length=1000)


class KnowledgeSourceCreate(BaseModel):
    title: str = Field(min_length=2, max_length=255)
    publisher: str | None = Field(default=None, max_length=120)
    edition: str | None = Field(default=None, max_length=80)
    location: str = Field(min_length=2, max_length=255)
    authorization_status: str = Field(pattern=r"^(authorized|self_owned|public_domain|internal_demo)$")


class KnowledgeNodeCreate(BaseModel):
    id: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9_\-]+$")
    name: str = Field(min_length=1, max_length=120)
    subject: str = Field(min_length=1, max_length=40)
    grade: str = Field(min_length=1, max_length=40)
    textbook_version: str = Field(min_length=1, max_length=80)
    chapter: str = Field(min_length=1, max_length=120)
    definition: str = Field(min_length=2, max_length=4000)
    explanation: str = Field(min_length=2, max_length=12000)
    common_errors: list[str] = Field(default_factory=list, max_length=30)
    question_types: list[str] = Field(default_factory=list, max_length=30)
    source_id: str
    source_excerpt: str = Field(min_length=2, max_length=255)
    review_status: str = Field(default="draft", pattern=r"^(draft|approved|archived)$")


class KnowledgeNodeUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    chapter: str | None = Field(default=None, min_length=1, max_length=120)
    definition: str | None = Field(default=None, min_length=2, max_length=4000)
    explanation: str | None = Field(default=None, min_length=2, max_length=12000)
    common_errors: list[str] | None = Field(default=None, max_length=30)
    question_types: list[str] | None = Field(default=None, max_length=30)
    source_id: str | None = None
    source_excerpt: str | None = Field(default=None, min_length=2, max_length=255)
    review_status: str | None = Field(default=None, pattern=r"^(draft|approved|archived)$")
    is_active: bool | None = None


class KnowledgeEdgeCreate(BaseModel):
    source_node_id: str
    target_node_id: str
    edge_type: EdgeType
    explanation: str = Field(min_length=2, max_length=255)


class KnowledgeEdgeUpdate(BaseModel):
    edge_type: EdgeType | None = None
    explanation: str | None = Field(default=None, min_length=2, max_length=255)


class KnowledgeEdgeResponse(ApiModel):
    id: str
    source_node_id: str
    target_node_id: str
    edge_type: EdgeType
    explanation: str


class KnowledgeNodeVersionResponse(ApiModel):
    id: str
    node_id: str
    version: int
    snapshot: dict
    change_note: str | None
    status: str
    created_by: str | None
    created_at: datetime
    published_at: datetime | None
    withdrawn_at: datetime | None


class KnowledgeNodeRestore(BaseModel):
    version: int = Field(ge=1)
    change_note: str | None = Field(default=None, max_length=255)
    publish: bool = False


class KnowledgeNodeAction(BaseModel):
    change_note: str | None = Field(default=None, max_length=255)


class AuditLogResponse(ApiModel):
    id: str
    actor_user_id: str | None
    action: str
    target_type: str
    target_id: str | None
    details: dict
    created_at: datetime


class ModelCostResponse(BaseModel):
    provider: str | None
    model: str | None
    calls: int
    input_tokens: int
    output_tokens: int
    credits: int


class AdminCreditAdjustment(BaseModel):
    amount: int = Field(ge=-100000, le=100000)
    reason: str = Field(min_length=2, max_length=255)


class HealthResponse(BaseModel):
    status: str
    database: str
    ai_provider: str
    ai_model: str
    ai_ready: bool
    retrieval_provider: str
    retrieval_model: str
    retrieval_ready: bool
