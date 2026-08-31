import pytest
from pydantic import ValidationError

from app.config import Settings


def test_pilot_is_a_supported_environment() -> None:
    settings = Settings(
        _env_file=None,
        app_env="pilot",
        jwt_secret="pilot-secret-that-is-not-a-development-default",
    )

    assert settings.app_env == "pilot"
    assert settings.privacy_consent_enforced is False


def test_ai_kill_switch_overrides_a_configured_provider() -> None:
    settings = Settings(_env_file=None, ai_enabled=False, ai_provider="stub")

    assert settings.ai_enabled is False
    assert settings.ai_ready is False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", []),
        ("[]", []),
        ("https://admin.example, https://teacher.example", ["https://admin.example", "https://teacher.example"]),
    ],
)
def test_cors_origins_accept_production_environment_formats(monkeypatch, raw: str, expected: list[str]) -> None:
    monkeypatch.setenv("CORS_ORIGINS", raw)

    settings = Settings(_env_file=None)

    assert settings.cors_origins == expected


@pytest.mark.parametrize("app_env", ["pilot", "production"])
def test_deployment_environments_reject_development_secret(app_env: str) -> None:
    with pytest.raises(ValidationError, match="JWT_SECRET must be changed"):
        Settings(
            _env_file=None,
            app_env=app_env,
            jwt_secret="development-only-secret-change-before-deploy",
        )


@pytest.mark.parametrize("app_env", ["pilot", "production"])
def test_school_features_require_pilot_authorization_gate(app_env: str) -> None:
    with pytest.raises(ValidationError, match="PILOT_AUTHORIZATION_ENFORCED"):
        Settings(
            _env_file=None,
            app_env=app_env,
            jwt_secret="release-secret-that-is-not-a-development-default",
            organizations_enabled=True,
            pilot_authorization_enforced=False,
        )


def test_contribution_rewards_require_contributions() -> None:
    with pytest.raises(ValidationError, match="CONTRIBUTIONS_ENABLED"):
        Settings(
            _env_file=None,
            contributions_enabled=False,
            contribution_rewards_enabled=True,
        )


def test_release_readiness_lists_missing_operational_gates() -> None:
    release = Settings(
        _env_file=None,
        app_env="production",
        jwt_secret="release-secret-that-is-not-a-development-default",
        privacy_consent_enforced=False,
        content_enforce_launch_scope=False,
        rate_limit_enabled=False,
        ai_provider="deepseek",
        deepseek_api_key=None,
        retrieval_provider="bailian",
        dashscope_api_key=None,
    )

    assert set(release.release_readiness_issues) == {
        "privacy_consent_not_enforced",
        "content_launch_scope_not_enforced",
        "rate_limit_not_enabled",
        "ai_provider_not_ready",
        "retrieval_provider_not_ready",
        "private_object_storage_not_ready",
    }
