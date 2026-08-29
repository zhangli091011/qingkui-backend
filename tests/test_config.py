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


@pytest.mark.parametrize("app_env", ["pilot", "production"])
def test_deployment_environments_reject_development_secret(app_env: str) -> None:
    with pytest.raises(ValidationError, match="JWT_SECRET must be changed"):
        Settings(
            _env_file=None,
            app_env=app_env,
            jwt_secret="development-only-secret-change-before-deploy",
        )
