"""Typed environment configuration."""

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
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
        populate_by_name=True,
        hide_input_in_errors=True,
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
        resolved_env = CustomEnvSettingsSource(settings_cls)
        # Empty APP placeholders in copied examples must not shadow SDK aliases.
        for source in (resolved_env, dotenv_settings):
            if isinstance(source, EnvSettingsSource):
                source.env_vars = {
                    name: value
                    for name, value in source.env_vars.items()
                    if not (
                        name.casefold().startswith("app_langsmith_")
                        and (value is None or not value.strip())
                    )
                }
        return (
            init_settings,
            resolved_env,
            dotenv_settings,
            file_secret_settings,
        )

    evidence_storage_root: Path = Path("~/.local/share/work-management/evidence").expanduser()

    reporting_timezone: str = "UTC"

    @field_validator("reporting_timezone")
    @classmethod
    def validate_reporting_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("reporting_timezone must be an IANA timezone") from exc
        return value

    name: str = "WorkCrew API"
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
    retrieval_enabled: bool = False
    retrieval_provider: Literal["disabled", "mock", "exa"] = "disabled"
    retrieval_core_exit_verified: bool = False
    exa_api_key: SecretStr | None = None
    progressive_planning_enabled: bool = False
    report_evaluation_hosted_enabled: bool = False
    report_evaluation_budget_tokens: int = Field(default=100000, ge=1, le=1000000)
    ai_model: str = ""
    ai_title_model: str = Field(default="gpt-4o-mini", min_length=1)
    ai_title_timeout_seconds: float = Field(default=3, gt=0, le=10)
    openai_api_key: SecretStr | None = None
    langsmith_tracing: bool = Field(
        default=False, validation_alias=AliasChoices("APP_LANGSMITH_TRACING", "LANGSMITH_TRACING")
    )
    langsmith_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("APP_LANGSMITH_API_KEY", "LANGSMITH_API_KEY")
    )
    langsmith_hide_inputs: bool = Field(
        default=True,
        validation_alias=AliasChoices("APP_LANGSMITH_HIDE_INPUTS", "LANGSMITH_HIDE_INPUTS"),
    )
    langsmith_hide_outputs: bool = Field(
        default=True,
        validation_alias=AliasChoices("APP_LANGSMITH_HIDE_OUTPUTS", "LANGSMITH_HIDE_OUTPUTS"),
    )
    langsmith_agent_id: str = Field(
        default="",
        pattern=r"^(?:[a-z][a-z0-9_-]{0,63})?$",
        validation_alias=AliasChoices("APP_LANGSMITH_AGENT_ID", "LANGSMITH_AGENT_ID"),
    )
    langsmith_agent_environment: Literal["local", "development", "staging", "production"] = Field(
        default="local",
        validation_alias=AliasChoices(
            "APP_LANGSMITH_AGENT_ENVIRONMENT", "LANGSMITH_AGENT_ENVIRONMENT"
        ),
    )
    langsmith_endpoint: str | None = Field(
        default=None, validation_alias=AliasChoices("APP_LANGSMITH_ENDPOINT", "LANGSMITH_ENDPOINT")
    )

    @field_validator("langsmith_endpoint")
    @classmethod
    def validate_langsmith_endpoint(cls, value: str | None) -> str | None:
        if not value:
            return None
        url = urlsplit(value)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError("LangSmith endpoint must be an HTTPS URL without credentials or query")
        return value.rstrip("/")

    @model_validator(mode="after")
    def require_langsmith_destination(self) -> "Settings":
        if self.langsmith_tracing and (
            not self.langsmith_agent_id
            or self.langsmith_api_key is None
            or not self.langsmith_api_key.get_secret_value().strip()
        ):
            raise ValueError("Enabled LangSmith tracing requires API key and agent ID")
        return self

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
