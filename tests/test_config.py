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


@pytest.mark.parametrize("app_env", ["pilot", "production"])
def test_deployment_environments_reject_development_secret(app_env: str) -> None:
    with pytest.raises(ValidationError, match="JWT_SECRET must be changed"):
        Settings(
            _env_file=None,
            app_env=app_env,
            jwt_secret="development-only-secret-change-before-deploy",
        )
