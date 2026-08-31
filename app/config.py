import json
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", ".env.local"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "青葵计划 API"
    app_env: Literal["development", "test", "pilot", "production"] = "development"
    api_prefix: str = "/api"
    database_url: str = "sqlite:///./qingkui.db"
    jwt_secret: str = "development-only-secret-change-before-deploy"
    access_token_minutes: int = 30
    refresh_token_days: int = 30
    initial_credits: int = 1280
    cors_origins: Annotated[list[str], NoDecode] = ["*"]
    password_reset_minutes: int = 30
    privacy_notice_version: str = "2026-08-31"
    privacy_consent_enforced: bool = False
    email_provider: Literal["disabled", "smtp", "console"] = "disabled"
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_from_address: str | None = None
    smtp_starttls: bool = True

    ai_enabled: bool = True
    ai_provider: Literal["deepseek", "stub"] = "deepseek"
    deepseek_api_key: str | None = None
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-flash"
    deepseek_timeout_seconds: float = 45.0
    deepseek_max_tokens: int = 4096

    retrieval_provider: Literal["bailian", "lexical"] = "lexical"
    dashscope_api_key: str | None = None
    dashscope_base_url: str = "https://dashscope.aliyuncs.com"
    dashscope_api_mode: Literal["native", "openai_compatible"] = "native"
    dashscope_embedding_model: str = "text-embedding-v4"
    dashscope_embedding_dimension: int = 1024
    dashscope_ocr_model: str = "qwen3.5-ocr"
    dashscope_rerank_model: str = "gte-rerank-v2"
    dashscope_rerank_enabled: bool = True
    dashscope_timeout_seconds: float = 30.0
    dashscope_query_timeout_seconds: float = 6.0
    retrieval_candidate_limit: int = 30
    retrieval_result_limit: int = 5
    retrieval_vector_index_path: str = "./qingkui-vectors.npz"
    retrieval_query_cache_size: int = 256
    reindex_max_workers: int = 4

    content_launch_subject: str = "数学"
    content_launch_grade: str = "高一"
    content_launch_textbook_version: str = "人教A版"
    content_enforce_launch_scope: bool = False

    # Optional Alibaba Cloud OSS migration/sync settings. Credentials are
    # intentionally read only from the process environment.
    oss_bucket: str | None = None
    oss_endpoint: str = "https://oss-cn-qingdao.aliyuncs.com"
    oss_access_key_id: str | None = None
    oss_access_key_secret: str | None = None
    oss_source_prefix: str = "knowledge/source"
    oss_vector_object_key: str = "knowledge/index/qingkui-vectors.npz"
    oss_vector_pointer_key: str = "knowledge/index/current.json"
    oss_user_content_prefix: str = "user-content/mistakes"
    oss_signed_url_seconds: int = 300

    redis_url: str = "redis://redis:6379/0"
    rate_limit_enabled: bool = False
    rate_limit_default_per_minute: int = 120
    rate_limit_auth_per_minute: int = 10
    rate_limit_ai_per_minute: int = 30
    rate_limit_upload_per_minute: int = 20
    rate_limit_trust_proxy_headers: bool = False
    operational_alert_window_minutes: int = 15
    operational_model_failure_min_calls: int = 5
    operational_model_failure_rate_threshold: float = 0.2
    operational_model_latency_threshold_ms: int = 30_000
    operational_ocr_failed_threshold: int = 3
    operational_ocr_stuck_minutes: int = 10
    operational_alert_webhook_url: str | None = None
    operational_alert_webhook_timeout_seconds: float = 5.0
    operational_alert_cooldown_minutes: int = 60
    ocr_queue_provider: Literal["local", "redis"] = "local"
    ocr_queue_name: str = "qingkui-ocr"
    ocr_max_attempts: int = 3
    ocr_review_threshold: float = 0.85
    user_upload_max_bytes: int = 10 * 1024 * 1024
    user_upload_max_pixels: int = 20_000_000
    organizations_enabled: bool = False
    pilot_authorization_enforced: bool = False
    idempotency_retention_hours: int = 24
    credit_campaigns_enabled: bool = False
    contributions_enabled: bool = False
    contribution_rewards_enabled: bool = False
    contribution_reward_delay_hours: int = 168
    contribution_queue_provider: Literal["local", "redis"] = "local"
    contribution_queue_name: str = "qingkui-contributions"

    @field_validator("cors_origins", mode="before")
    @classmethod
    def parse_origins(cls, value: object) -> object:
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return []
            if raw.startswith("["):
                return json.loads(raw)
            return [item.strip() for item in raw.split(",") if item.strip()]
        return value

    @model_validator(mode="after")
    def validate_production_secrets(self) -> "Settings":
        if self.app_env in {"pilot", "production"} and self.jwt_secret.startswith("development-"):
            raise ValueError("JWT_SECRET must be changed in production")
        if (
            self.app_env in {"pilot", "production"}
            and self.organizations_enabled
            and not self.pilot_authorization_enforced
        ):
            raise ValueError("PILOT_AUTHORIZATION_ENFORCED must be enabled before school features")
        if self.contribution_rewards_enabled and not self.contributions_enabled:
            raise ValueError("CONTRIBUTIONS_ENABLED must be enabled before contribution rewards")
        return self

    @property
    def ai_ready(self) -> bool:
        return self.ai_enabled and (self.ai_provider == "stub" or bool(self.deepseek_api_key))

    @property
    def retrieval_ready(self) -> bool:
        return self.retrieval_provider == "lexical" or bool(self.dashscope_api_key)

    @property
    def object_storage_ready(self) -> bool:
        return bool(self.oss_bucket and self.oss_access_key_id and self.oss_access_key_secret)

    @property
    def release_readiness_issues(self) -> list[str]:
        if self.app_env not in {"pilot", "production"}:
            return []
        issues: list[str] = []
        if not self.privacy_consent_enforced:
            issues.append("privacy_consent_not_enforced")
        if not self.content_enforce_launch_scope:
            issues.append("content_launch_scope_not_enforced")
        if not self.rate_limit_enabled:
            issues.append("rate_limit_not_enabled")
        if self.ai_enabled and not self.ai_ready:
            issues.append("ai_provider_not_ready")
        if not self.retrieval_ready:
            issues.append("retrieval_provider_not_ready")
        if not self.object_storage_ready:
            issues.append("private_object_storage_not_ready")
        return issues


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
