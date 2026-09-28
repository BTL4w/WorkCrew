"""Typed environment configuration."""

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    EnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

_POSTGRESQL_ASYNC_URL_PREFIX = "postgresql+psycopg://"


class CustomEnvSettingsSource(EnvSettingsSource):
    """Env settings source that handles empty string values for complex fields."""

    def decode_complex_value(self, field_name: str, field: Any, value: Any) -> Any:
        if value is None or (isinstance(value, str) and not value.strip().strip("'\"")):
            return []
        if isinstance(value, str):
            clean_value = value.strip()
            if (clean_value.startswith("'") and clean_value.endswith("'")) or (
                clean_value.startswith('"') and clean_value.endswith('"')
            ):
                clean_value = clean_value[1:-1].strip()
            if not clean_value:
                return []
            value = clean_value
        return super().decode_complex_value(field_name, field, value)


class Settings(BaseSettings):
    """Runtime settings loaded from environment variables or a local .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="APP_",
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            CustomEnvSettingsSource(settings_cls),
            dotenv_settings,
            file_secret_settings,
        )

    evidence_storage_root: Path = Path("~/.local/share/work-management/evidence").expanduser()

    name: str = "Work Management API"
    version: str = "0.1.0"
    environment: Literal["local", "test", "production"] = "local"
    debug: bool = False
    database_url: str = (
        "postgresql+psycopg://work_management:work_management@localhost:5432/work_management"
    )
    local_auth_organization_slug: str = "demo"
    local_auth_organization_name: str = "Demo Organization"
    demo_seed_enabled: bool = False
    demo_seed_password: SecretStr = SecretStr("WorkDemo123!")
    session_cookie_name: str = "work_management_session"
    session_ttl_seconds: int = Field(default=28_800, ge=300, le=604_800)
    session_secure_cookie: bool = False
    frontend_origin: str = "http://localhost:3000"
    ai_provider: Literal["disabled", "mock", "openai"] = "disabled"
    ai_model: str = ""
    ai_title_model: str = Field(default="gpt-4o-mini", min_length=1)
    ai_title_timeout_seconds: float = Field(default=3, gt=0, le=10)
    openai_api_key: SecretStr | None = None
    langsmith_tracing: bool = False
    langsmith_api_key: SecretStr | None = None
    ai_raw_context_retention_days: int = Field(default=30, ge=0, le=30)
    ai_redacted_trace_retention_days: int = Field(default=90, ge=0, le=90)
    worker_poll_interval_seconds: float = Field(default=0.5, ge=0.1, le=10)
    worker_lease_seconds: int = Field(default=60, ge=10, le=600)
    worker_organization_ids: list[UUID] = Field(default_factory=list)  # type: ignore
    worker_id: str = ""

    @field_validator("worker_organization_ids", mode="before")
    @classmethod
    def parse_worker_organization_ids(cls, value: object) -> object:
        """Parse string values or handle empty string from environment."""
        if isinstance(value, str) and not value.strip():
            return []
        return value

    @field_validator("database_url")
    @classmethod
    def require_async_postgresql_driver(cls, value: str) -> str:
        """Reject database URLs that bypass the selected PostgreSQL async driver."""

        if not value.startswith(_POSTGRESQL_ASYNC_URL_PREFIX):
            msg = f"database_url must start with {_POSTGRESQL_ASYNC_URL_PREFIX}"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def require_secure_production_cookie(self) -> "Settings":
        """Refuse a production configuration that sends the session cookie over HTTP."""

        if self.environment == "production" and not self.session_secure_cookie:
            msg = "session_secure_cookie must be true in production"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def require_complete_production_openai_configuration(self) -> "Settings":
        """Require hosted-model identity and credentials only when activated."""

        api_key = self.openai_api_key
        has_api_key = api_key is not None and bool(api_key.get_secret_value().strip())
        if (
            self.environment == "production"
            and self.ai_provider == "openai"
            and (not self.ai_model.strip() or not has_api_key)
        ):
            msg = "OpenAI production configuration requires a model and API key"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def generate_default_worker_id(self) -> "Settings":
        """Generate a stable worker ID from hostname and PID if not configured."""

        if not self.worker_id.strip():
            import os
            import socket

            object.__setattr__(self, "worker_id", f"worker-{socket.gethostname()}-{os.getpid()}")
        return self


@lru_cache
def get_settings() -> Settings:
    """Return one immutable-by-convention settings object per process."""

    return Settings()
