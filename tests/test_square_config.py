import pytest

from integrations.square.config import SquareConfig, SquareEnvironment
from integrations.square.exceptions import (
    SquareConfigurationError,
    SquareProductionWriteBlocked,
)


def clear_square_environment(monkeypatch):
    for name in (
        "SQUARE_ENVIRONMENT",
        "SQUARE_SANDBOX_ACCESS_TOKEN",
        "SQUARE_PRODUCTION_ACCESS_TOKEN",
        "SQUARE_LOCATION_ID",
        "SQUARE_API_VERSION",
        "SQUARE_REQUEST_TIMEOUT_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)


def test_sandbox_url_selection(monkeypatch):
    clear_square_environment(monkeypatch)
    monkeypatch.setenv("SQUARE_ENVIRONMENT", "sandbox")
    config = SquareConfig.from_env()
    assert config.environment is SquareEnvironment.SANDBOX
    assert config.base_url == "https://connect.squareupsandbox.com"


def test_production_url_selection(monkeypatch):
    clear_square_environment(monkeypatch)
    monkeypatch.setenv("SQUARE_ENVIRONMENT", "production")
    config = SquareConfig.from_env()
    assert config.environment is SquareEnvironment.PRODUCTION
    assert config.base_url == "https://connect.squareup.com"


def test_missing_sandbox_token_has_safe_error():
    config = SquareConfig(environment=SquareEnvironment.SANDBOX)
    with pytest.raises(SquareConfigurationError, match="SQUARE_SANDBOX_ACCESS_TOKEN"):
        _ = config.access_token


def test_invalid_environment_is_rejected(monkeypatch):
    clear_square_environment(monkeypatch)
    monkeypatch.setenv("SQUARE_ENVIRONMENT", "staging")
    with pytest.raises(SquareConfigurationError, match="sandbox.*production"):
        SquareConfig.from_env()


def test_production_writes_are_always_blocked():
    config = SquareConfig(
        environment=SquareEnvironment.PRODUCTION,
        production_access_token="production-secret",
    )
    with pytest.raises(SquareProductionWriteBlocked, match="disabled"):
        config.assert_write_allowed()

