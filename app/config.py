from functools import lru_cache
from typing import Literal

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", ".env.local"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "青葵计划 API"
    app_env: Literal["development", "test", "production"] = "development"
    api_prefix: str = "/api"
    database_url: str = "sqlite:///./qingkui.db"
    jwt_secret: str = "development-only-secret-change-before-deploy"
    access_token_minutes: int = 30
    refresh_token_days: int = 30
    initial_credits: int = 1280
    cors_origins: list[str] = ["*"]

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

    # Optional Alibaba Cloud OSS migration/sync settings. Credentials are
    # intentionally read only from the process environment.
    oss_bucket: str | None = None
    oss_endpoint: str = "https://oss-cn-qingdao.aliyuncs.com"
    oss_access_key_id: str | None = None
    oss_access_key_secret: str | None = None
    oss_source_prefix: str = "knowledge/source"
    oss_vector_object_key: str = "knowledge/index/qingkui-vectors.npz"

    @field_validator("cors_origins", mode="before")
    @classmethod
    def parse_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @model_validator(mode="after")
    def validate_production_secrets(self) -> "Settings":
        if self.app_env == "production" and self.jwt_secret.startswith("development-"):
            raise ValueError("JWT_SECRET must be changed in production")
        return self

    @property
    def ai_ready(self) -> bool:
        return self.ai_provider == "stub" or bool(self.deepseek_api_key)

    @property
    def retrieval_ready(self) -> bool:
        return self.retrieval_provider == "lexical" or bool(self.dashscope_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
