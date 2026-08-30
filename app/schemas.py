from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

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


class PasswordResetRequest(BaseModel):
    email: EmailStr


class PasswordResetConfirm(BaseModel):
    token: str = Field(min_length=32, max_length=256)
    new_password: str = Field(min_length=8, max_length=128)


class PasswordResetRequestResponse(BaseModel):
    message: str
    reset_token: str | None = None


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


class DeviceSessionResponse(BaseModel):
    id: str
    device_name: str | None
    expires_at: datetime
    revoked_at: datetime | None
    created_at: datetime
    active: bool


class SchoolCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    code: str = Field(min_length=2, max_length=40, pattern=r"^[a-zA-Z0-9_-]+$")


class SchoolResponse(ApiModel):
    id: str
    name: str
    code: str
    status: str
    created_at: datetime


class SchoolMembershipResponse(ApiModel):
    id: str
    school_id: str
    role: str
    status: str
    joined_at: datetime
    school: SchoolResponse


class OrganizationMeResponse(BaseModel):
    memberships: list[SchoolMembershipResponse]


class SchoolClassCreate(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    grade: str | None = Field(default=None, max_length=40)
    academic_year: str = Field(min_length=4, max_length=20)


class SchoolClassResponse(ApiModel):
    id: str
    school_id: str
    name: str
    grade: str | None
    academic_year: str
    status: str
    created_at: datetime


class OrganizationInviteCreate(BaseModel):
    class_id: str | None = Field(default=None, max_length=36)
    member_role: str = Field(pattern=r"^(teacher|student)$")
    max_uses: int = Field(default=1, ge=1, le=500)
    expires_hours: int = Field(default=72, ge=1, le=24 * 30)


class OrganizationInviteResponse(ApiModel):
    id: str
    school_id: str
    class_id: str | None
    member_role: str
    max_uses: int
    use_count: int
    expires_at: datetime
    is_active: bool
    code: str


class OrganizationInviteRedeem(BaseModel):
    code: str = Field(min_length=8, max_length=128)


class OrganizationJoinResponse(BaseModel):
    school: SchoolResponse
    school_role: str
    classroom: SchoolClassResponse | None
    joined: bool


class ClassStudentOverview(BaseModel):
    anonymous_id: str
    joined_at: datetime
    last_activity_at: datetime | None
    questions: int
    mistakes: int
    verified_nodes: int


class ClassOverviewResponse(BaseModel):
    classroom: SchoolClassResponse
    student_count: int
    active_7d_students: int
    questions: int
    mistakes: int
    verified_nodes: int
    students: list[ClassStudentOverview]


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
    section: str = "本章知识点"
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


class KnowledgeCatalogItem(BaseModel):
    subject: str
    grade: str
    textbook_version: str
    node_count: int


class KnowledgeTreeNode(BaseModel):
    id: str
    name: str
    status: KnowledgeStatus


class KnowledgeTreeSection(BaseModel):
    name: str
    nodes: list[KnowledgeTreeNode]


class KnowledgeTreeChapter(BaseModel):
    name: str
    sections: list[KnowledgeTreeSection]


class KnowledgeTreeResponse(BaseModel):
    subject: str
    grade: str
    textbook_version: str
    chapters: list[KnowledgeTreeChapter]


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


class LearningCheckChoice(BaseModel):
    id: str
    text: str


class LearningCheckResponse(ApiModel):
    id: str
    node_id: str
    prompt: str
    choices: list[LearningCheckChoice]
    status: str
    expires_at: datetime


class LearningCheckSubmit(BaseModel):
    choice_id: str = Field(min_length=8, max_length=64)


class LearningCheckResult(BaseModel):
    attempt_id: str
    passed: bool
    status: str
    state: "LearningSummaryItem"


class LearningSummaryItem(KnowledgeNodeSummary):
    updated_at: datetime
    note: str | None
    is_favorite: bool


class LearningSummary(BaseModel):
    recent: list[LearningSummaryItem]
    review: list[LearningSummaryItem]
    error_prone: list[LearningSummaryItem]
    verified: list[LearningSummaryItem]


class MistakeCreate(BaseModel):
    subject: str | None = Field(default=None, max_length=40)
    question_text: str | None = Field(default=None, max_length=12000)
    student_work: str | None = Field(default=None, max_length=12000)
    question_goal: str | None = Field(default=None, max_length=1000)
    error_category: str | None = Field(default=None, pattern=r"^(concept|reading|method|calculation|expression)$")


class MistakeUpdate(BaseModel):
    subject: str | None = Field(default=None, max_length=40)
    question_text: str | None = Field(default=None, max_length=12000)
    corrected_text: str | None = Field(default=None, max_length=12000)
    student_work: str | None = Field(default=None, max_length=12000)
    question_goal: str | None = Field(default=None, max_length=1000)
    error_category: str | None = Field(default=None, pattern=r"^(concept|reading|method|calculation|expression)$")
    error_note: str | None = Field(default=None, max_length=2000)
    knowledge_node_id: str | None = Field(default=None, max_length=64)
    study_status: str | None = Field(default=None, pattern=r"^(active|reviewing|mastered|archived)$")


class MistakeAssetResponse(ApiModel):
    id: str
    mime_type: str
    size_bytes: int
    width: int
    height: int
    status: str
    created_at: datetime


class OcrTaskResponse(ApiModel):
    id: str
    asset_id: str
    status: str
    attempts: int
    result_text: str | None
    formulas: list[dict]
    confidence: float | None
    requires_review: bool
    error_code: str | None
    error_message: str | None
    queued_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime


class MistakePracticeResponse(ApiModel):
    id: str
    round_id: str | None
    position: int | None
    question_text: str
    hint: str | None
    answer_reference: str | None
    source: str
    status: str
    student_answer: str | None
    is_correct: bool | None
    validation_details: dict
    created_at: datetime
    completed_at: datetime | None


class MistakePracticeRoundResponse(ApiModel):
    id: str
    round_number: int
    review_stage: str
    status: str
    question_count: int
    correct_count: int
    authoritative_correct_count: int
    started_at: datetime
    completed_at: datetime | None
    practices: list[MistakePracticeResponse] = Field(default_factory=list)


class MistakeResponse(ApiModel):
    id: str
    subject: str | None
    question_text: str | None
    corrected_text: str | None
    student_work: str | None
    question_goal: str | None
    error_category: str | None
    error_note: str | None
    analysis: dict
    analysis_status: str
    analysis_provider: str | None
    analysis_model: str | None
    analyzed_at: datetime | None
    knowledge_node_id: str | None
    link_status: str
    review_status: str
    study_status: str
    next_review_at: datetime | None
    attempt_count: int
    review_stage: str
    first_corrected_at: datetime | None
    last_reviewed_at: datetime | None
    second_attempt_correct: bool | None
    review_streak: int
    created_at: datetime
    updated_at: datetime
    assets: list[MistakeAssetResponse] = Field(default_factory=list)
    ocr_tasks: list[OcrTaskResponse] = Field(default_factory=list)
    practices: list[MistakePracticeResponse] = Field(default_factory=list)
    practice_rounds: list[MistakePracticeRoundResponse] = Field(default_factory=list)


class OcrCorrection(BaseModel):
    corrected_text: str = Field(min_length=1, max_length=12000)


class PracticeCreate(BaseModel):
    question_text: str | None = Field(default=None, max_length=12000)
    answer_reference: str | None = Field(default=None, max_length=12000)


class PracticeSubmit(BaseModel):
    student_answer: str = Field(min_length=1, max_length=12000)
    is_correct: bool | None = None
    validation_details: dict = Field(default_factory=dict)


class SimilarPracticeData(BaseModel):
    question: str = Field(min_length=1, max_length=12000)
    hint: str = Field(min_length=1, max_length=2000)
    answer_reference: str = Field(min_length=1, max_length=12000)


class MistakeAnalysisData(BaseModel):
    diagnosis: str = Field(min_length=1, max_length=4000)
    error_category: str = Field(pattern=r"^(concept|reading|method|calculation|expression)$")
    error_note: str = Field(min_length=1, max_length=2000)
    correction_steps: list[str] = Field(default_factory=list, max_length=8)
    suggested_node_id: str | None = Field(default=None, max_length=64)
    node_confidence: float = Field(default=0, ge=0, le=1)
    similar_question: str = Field(min_length=1, max_length=12000)
    answer_reference: str = Field(min_length=1, max_length=12000)
    similar_practices: list[SimilarPracticeData] = Field(default_factory=list, max_length=3)
    uncertain: bool = False


class MistakeAnalysisResponse(BaseModel):
    mistake_id: str
    analysis: MistakeAnalysisData
    credits_charged: int
    balance: int
    provider: str
    model: str


class WeeklyMistakeLink(BaseModel):
    mistake_id: str
    practice_round_id: str | None = None
    knowledge_node_id: str | None = None
    title: str
    review_stage: str
    next_review_at: datetime | None = None


class MistakeWeeklyReview(BaseModel):
    week_start: date
    week_end: date
    new_mistakes: int
    error_categories: dict[str, int]
    weak_knowledge_points: list[dict]
    due_reviews: list[WeeklyMistakeLink]
    practice_completion_rate: float
    authoritative_accuracy: float
    second_attempt_accuracy: float
    seven_day_followup_rate: float


class ConversationCreate(BaseModel):
    mode: QaMode = QaMode.knowledge
    knowledge_node_id: str | None = None
    subject: str | None = Field(default=None, min_length=1, max_length=40)
    title: str | None = Field(default=None, max_length=120)


class ConversationMessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=12000)
    help_level: HelpLevel = HelpLevel.approach


class QaIntentRequest(BaseModel):
    content: str = Field(min_length=1, max_length=12000)
    mode: QaMode = QaMode.knowledge


class QaIntentOption(BaseModel):
    id: str
    label: str
    instruction: str
    mode: QaMode


class QaIntentResult(BaseModel):
    needs_clarification: bool
    subject: str | None = None
    prompt: str | None = None
    options: list[QaIntentOption] = Field(default_factory=list)


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


class CreditCampaignCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    amount: int = Field(ge=1, le=100_000)
    school_id: str | None = Field(default=None, max_length=36)
    starts_at: datetime | None = None
    expires_hours: int = Field(default=24 * 30, ge=1, le=24 * 365)
    max_redemptions: int = Field(ge=1, le=100_000)
    per_user_limit: int = Field(default=1, ge=1, le=100)
    code_count: int = Field(default=1, ge=1, le=1000)
    code_max_uses: int = Field(default=1, ge=1, le=100_000)


class CreditCampaignResponse(ApiModel):
    id: str
    name: str
    amount: int
    school_id: str | None
    status: str
    starts_at: datetime
    ends_at: datetime
    max_redemptions: int
    redemption_count: int
    per_user_limit: int
    created_at: datetime


class CreditCampaignCreatedResponse(CreditCampaignResponse):
    codes: list[str]


class CreditRedeemRequest(BaseModel):
    code: str = Field(min_length=8, max_length=128)


class CreditRedeemResponse(BaseModel):
    campaign_id: str
    campaign_name: str
    amount: int
    balance: int


class CreditRedemptionResponse(BaseModel):
    id: str
    campaign_id: str
    campaign_name: str
    amount: int
    created_at: datetime


class ContributionCreate(BaseModel):
    contribution_type: str = Field(pattern=r"^(correction|explanation|question|source)$")
    title: str = Field(min_length=2, max_length=160)
    content: str = Field(min_length=20, max_length=20_000)
    source_reference: str | None = Field(default=None, max_length=1000)


class ContributionResponse(ApiModel):
    id: str
    school_id: str | None
    contribution_type: str
    title: str
    content: str
    source_reference: str | None
    status: str
    ai_review: dict
    ai_provider: str | None
    ai_model: str | None
    review_note: str | None
    reward_amount: int
    reward_status: str
    reward_available_at: datetime | None
    created_at: datetime
    updated_at: datetime


class AdminContributionResponse(ContributionResponse):
    user_id: str
    reviewed_by: str | None
    reviewed_at: datetime | None


class ContributionReview(BaseModel):
    decision: str = Field(pattern=r"^(approved|rejected)$")
    review_note: str = Field(min_length=2, max_length=2000)
    reward_amount: int = Field(default=0, ge=0, le=100_000)


class ContributionSettlementResponse(BaseModel):
    settled_count: int
    total_credits: int


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
    review_note: str | None = None
    reviewed_at: datetime | None = None
    created_at: datetime


class AdminFeedbackResponse(FeedbackResponse):
    user_id: str
    reviewed_by: str | None = None


class FeedbackReview(BaseModel):
    status: str = Field(pattern=r"^(pending|accepted|rejected|resolved)$")
    review_note: str | None = Field(default=None, max_length=1000)


class AdminUserResponse(BaseModel):
    id: str
    username: str
    email: str | None
    nickname: str
    role: UserRole
    tenant_id: str | None
    is_active: bool
    balance: int | None
    created_at: datetime
    deleted_at: datetime | None


class AdminUserStatusUpdate(BaseModel):
    is_active: bool


class AdminUserRoleUpdate(BaseModel):
    role: UserRole


class PilotMetricsResponse(BaseModel):
    start_at: datetime
    end_at: datetime
    registered_users: int
    activated_users: int
    activation_rate: float
    retention_7d_eligible_users: int
    retained_7d_users: int
    retention_7d_rate: float
    helpful_votes: int
    unhelpful_votes: int
    answer_helpfulness_rate: float | None
    graph_explorers: int
    graph_exploration_rate: float
    knowledge_state_changes: int
    credits_spent: int
    average_credits_per_active_user: float


class PilotUserExport(BaseModel):
    anonymous_id: str
    registered_at: datetime
    activated: bool
    last_activity_at: datetime | None
    conversation_count: int
    assistant_message_count: int
    graph_exploration_count: int
    knowledge_state_change_count: int
    verified_node_count: int
    credits_spent: int
    helpful_votes: int
    unhelpful_votes: int
    mistake_count: int
    completed_practice_count: int


class PilotExportResponse(BaseModel):
    generated_at: datetime
    metrics: PilotMetricsResponse
    users: list[PilotUserExport]
    privacy_note: str


class PilotCleanupRequest(BaseModel):
    user_ids: list[str] = Field(min_length=1, max_length=100)

    @field_validator("user_ids")
    @classmethod
    def unique_user_ids(cls, value: list[str]) -> list[str]:
        normalized = [item.strip() for item in value if item.strip()]
        if len(normalized) != len(value) or len(set(normalized)) != len(normalized):
            raise ValueError("用户 ID 不能为空或重复")
        return normalized


class PilotCleanupExecute(PilotCleanupRequest):
    confirmation: str
    expected_count: int = Field(ge=1, le=100)


class PilotCleanupCandidate(BaseModel):
    user_id: str
    anonymous_id: str
    eligible: bool
    reason: str | None
    asset_count: int


class PilotCleanupPreview(BaseModel):
    requested_count: int
    eligible_count: int
    blocked_count: int
    candidates: list[PilotCleanupCandidate]


class PilotCleanupResult(BaseModel):
    deleted_count: int
    anonymous_ids: list[str]


class AdminOcrTaskResponse(OcrTaskResponse):
    mistake_id: str
    user_id: str


class AdminOcrTaskDetail(AdminOcrTaskResponse):
    subject: str | None
    question_text: str | None
    corrected_text: str | None
    student_work: str | None
    question_goal: str | None
    asset_mime_type: str
    asset_size_bytes: int


class AdminMistakeResponse(MistakeResponse):
    user_id: str


class AdminMistakeReview(BaseModel):
    review_status: str | None = Field(default=None, pattern=r"^(draft|needs_review|recognized|confirmed|approved|rejected)$")
    error_category: str | None = Field(default=None, pattern=r"^(concept|reading|method|calculation|expression|other)$")
    error_note: str | None = Field(default=None, max_length=2000)
    knowledge_node_id: str | None = None
    link_status: str | None = Field(default=None, pattern=r"^(pending|confirmed|rejected)$")


class KnowledgeSourceCreate(BaseModel):
    title: str = Field(min_length=2, max_length=255)
    publisher: str | None = Field(default=None, max_length=120)
    edition: str | None = Field(default=None, max_length=80)
    location: str = Field(min_length=2, max_length=255)
    authorization_status: str = Field(pattern=r"^(authorized|self_owned|public_domain|internal_demo)$")


class KnowledgeSourceUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=2, max_length=255)
    publisher: str | None = Field(default=None, max_length=120)
    edition: str | None = Field(default=None, max_length=80)
    location: str | None = Field(default=None, min_length=2, max_length=255)
    authorization_status: str | None = Field(
        default=None,
        pattern=r"^(authorized|self_owned|public_domain|internal_demo)$",
    )


class AdminKnowledgeDocumentResponse(ApiModel):
    id: str
    title: str
    subject: str | None
    grade: str | None
    textbook_version: str | None
    chapter: str | None
    document_role: str | None
    source_type: str
    source_uri: str
    authorization_status: str
    checksum_sha256: str
    mime_type: str | None
    status: str
    error_message: str | None
    document_metadata: dict
    chunk_count: int
    formula_count: int
    pending_formula_count: int
    created_at: datetime
    updated_at: datetime


class AdminKnowledgeDocumentListResponse(BaseModel):
    total: int
    offset: int
    limit: int
    items: list[AdminKnowledgeDocumentResponse]


class AdminKnowledgeDocumentUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=2, max_length=255)
    subject: str | None = Field(default=None, min_length=1, max_length=40)
    grade: str | None = Field(default=None, min_length=1, max_length=40)
    textbook_version: str | None = Field(default=None, min_length=1, max_length=80)
    chapter: str | None = Field(default=None, min_length=1, max_length=120)
    document_role: str | None = Field(default=None, min_length=1, max_length=40)
    authorization_status: str | None = Field(
        default=None,
        pattern=r"^(authorized|self_owned|public_domain|pending_review)$",
    )
    status: str | None = Field(
        default=None,
        pattern=r"^(processing|text_ready|failed|archived)$",
    )


class KnowledgeGraphIntegrityReport(BaseModel):
    node_count: int
    edge_count: int
    orphaned_node_ids: list[str]
    inactive_edge_ids: list[str]
    cross_subject_edge_ids: list[str]
    self_referential_edge_ids: list[str]
    duplicate_node_groups: list[list[str]]
    prerequisite_cycles: list[list[str]]


class KnowledgeNodeCreate(BaseModel):
    id: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9_\-]+$")
    name: str = Field(min_length=1, max_length=120)
    subject: str = Field(min_length=1, max_length=40)
    grade: str = Field(min_length=1, max_length=40)
    textbook_version: str = Field(min_length=1, max_length=80)
    chapter: str = Field(min_length=1, max_length=120)
    section: str = Field(default="本章知识点", min_length=1, max_length=120)
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
    section: str | None = Field(default=None, min_length=1, max_length=120)
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


class ContentGovernanceCandidate(BaseModel):
    id: str
    name: str
    chapter: str
    review_status: str
    publishable: bool
    blockers: list[str]


class ContentGovernanceReport(BaseModel):
    scope: dict[str, str]
    nodes: dict[str, int]
    documents: dict[str, int]
    relations: dict[str, int]
    blocker_counts: dict[str, int]
    candidates: list[ContentGovernanceCandidate]


class KnowledgeReviewQueueItem(BaseModel):
    id: str
    name: str
    subject: str
    grade: str
    textbook_version: str
    chapter: str
    review_status: str
    is_active: bool
    source_title: str
    source_excerpt: str
    blockers: list[str]
    updated_at: datetime


class KnowledgeReviewQueueResponse(BaseModel):
    total: int
    offset: int
    limit: int
    items: list[KnowledgeReviewQueueItem]


class FormulaReviewItem(BaseModel):
    id: str
    document_id: str
    document_title: str
    subject: str | None
    chapter: str | None
    sequence: int
    formula_latex: str | None
    formula_source: str | None
    ocr_confidence: float | None
    review_status: str | None
    review_note: str | None


class FormulaReviewQueueResponse(BaseModel):
    total: int
    offset: int
    limit: int
    items: list[FormulaReviewItem]


class FormulaReviewUpdate(BaseModel):
    review_status: str = Field(pattern=r"^(pending|approved|rejected)$")
    formula_latex: str | None = Field(default=None, min_length=1, max_length=12000)
    review_note: str | None = Field(default=None, max_length=1000)


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
    successful_calls: int = 0
    failed_calls: int = 0
    failure_rate: float = 0
    average_latency_ms: float = 0


class OperationalAlert(BaseModel):
    severity: str
    code: str
    message: str
    value: float
    threshold: float


class OperationalAlertSummary(BaseModel):
    status: str
    generated_at: datetime
    window_start: datetime
    metrics: dict[str, float | int]
    alerts: list[OperationalAlert]


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
