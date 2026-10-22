"""Retrieval configuration is opt-in and independent of model configuration."""

from pathlib import Path
from typing import Literal

import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import Settings


@pytest.fixture(autouse=True)
def isolate_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    for name in (
        "APP_RETRIEVAL_ENABLED",
        "APP_RETRIEVAL_PROVIDER",
        "APP_RETRIEVAL_CORE_EXIT_VERIFIED",
        "APP_EXA_API_KEY",
        "APP_AI_PROVIDER",
    ):
        monkeypatch.delenv(name, raising=False)


def test_retrieval_defaults_off_and_key_does_not_activate_it() -> None:
    settings = Settings(exa_api_key=SecretStr("test-only-key"))
    assert settings.retrieval_enabled is False
    assert settings.retrieval_provider == "disabled"
    assert settings.retrieval_core_exit_verified is False
    assert settings.exa_api_key is not None
    assert "test-only-key" not in repr(settings)


@pytest.mark.parametrize("provider", ["disabled", "mock", "exa"])
def test_provider_is_separate_from_main_and_title_models(
    provider: Literal["disabled", "mock", "exa"],
) -> None:
    settings = Settings(retrieval_provider=provider)
    assert settings.retrieval_provider == provider
    assert settings.ai_provider == "disabled"


def test_retrieval_settings_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_RETRIEVAL_ENABLED", "true")
    monkeypatch.setenv("APP_RETRIEVAL_PROVIDER", "exa")
    monkeypatch.setenv("APP_RETRIEVAL_CORE_EXIT_VERIFIED", "true")
    monkeypatch.setenv("APP_EXA_API_KEY", "test-only-env-key")
    settings = Settings()
    assert settings.retrieval_enabled
    assert settings.retrieval_core_exit_verified
    assert settings.exa_api_key == SecretStr("test-only-env-key")


def test_unknown_retrieval_provider_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_RETRIEVAL_PROVIDER", "anything")
    with pytest.raises(ValidationError):
        Settings()
