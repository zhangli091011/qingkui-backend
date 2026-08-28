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


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
