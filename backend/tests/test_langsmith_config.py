"""Tracing configuration is explicit, secret-safe and usable without shell exports."""

import json
from pathlib import Path
from uuid import uuid4

import pytest
from langsmith import Client
from pydantic import SecretStr, ValidationError

from app.core.config import Settings
from app.core.tracing import build_trace_adapter
from work_management_ai.tracing import trace_session, trace_span


@pytest.fixture(autouse=True)
def isolated_configuration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)


def test_disabled_tracing_does_not_construct_a_hosted_client() -> None:
    assert build_trace_adapter(Settings()) is None


def test_sdk_environment_aliases_are_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "test-local-key")
    monkeypatch.setenv("LANGSMITH_AGENT_ID", "workcrew")
    monkeypatch.setenv("LANGSMITH_AGENT_ENVIRONMENT", "local")
    settings = Settings()
    assert settings.langsmith_tracing is True
    assert settings.langsmith_agent_id == "workcrew"
    assert settings.langsmith_agent_environment == "local"


def test_visibility_flags_accept_sdk_aliases_and_app_precedence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LANGSMITH_HIDE_INPUTS", "false")
    monkeypatch.setenv("LANGSMITH_HIDE_OUTPUTS", "false")
    settings = Settings()
    assert settings.langsmith_hide_inputs is False
    assert settings.langsmith_hide_outputs is False
    monkeypatch.setenv("APP_LANGSMITH_HIDE_INPUTS", "true")
    assert Settings().langsmith_hide_inputs is True


def test_visibility_defaults_hide_both_directions() -> None:
    settings = Settings()
    assert settings.langsmith_hide_inputs and settings.langsmith_hide_outputs


def test_empty_app_placeholders_do_not_shadow_sdk_aliases(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "APP_LANGSMITH_TRACING=true\nAPP_LANGSMITH_API_KEY=\n"
        "APP_LANGSMITH_AGENT_ID=\nAPP_LANGSMITH_ENDPOINT=\n"
        "LANGSMITH_API_KEY=dummy-local-key\nLANGSMITH_AGENT_ID=workcrew\n"
        "LANGSMITH_ENDPOINT=https://eu.api.smith.langchain.com\n"
    )
    settings = Settings()
    assert settings.langsmith_api_key is not None
    assert settings.langsmith_api_key.get_secret_value() == "dummy-local-key"
    assert settings.langsmith_agent_id == "workcrew"
    assert settings.langsmith_endpoint == "https://eu.api.smith.langchain.com"


def test_application_toggle_takes_precedence_over_sdk_toggle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    assert Settings().langsmith_tracing is False


@pytest.mark.parametrize(
    "endpoint",
    ["http://example.test", "https://user:pass@example.test", "https://example.test/?key=abc"],
)
def test_endpoint_validation_does_not_echo_secrets(endpoint: str) -> None:
    with pytest.raises(ValidationError) as captured:
        Settings(langsmith_endpoint=endpoint)
    assert endpoint not in str(captured.value)


def test_enabled_configuration_requires_agent_destination_without_echoing_key() -> None:
    with pytest.raises(ValidationError) as captured:
        Settings(langsmith_tracing=True, langsmith_api_key=SecretStr("private-key"))
    assert "private-key" not in str(captured.value)


def test_client_construction_failure_preserves_manual_runtime(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def fail(**kwargs: object) -> None:
        raise RuntimeError("secret raw provider details")

    monkeypatch.setattr("app.core.tracing.Client", fail)
    settings = Settings(
        langsmith_tracing=True,
        langsmith_api_key=SecretStr("private-key"),
        langsmith_agent_id="workcrew",
        langsmith_agent_environment="local",
    )
    assert build_trace_adapter(settings) is None
    assert "secret raw provider details" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("hidden", [True, False])
async def test_real_sdk_serializes_only_allowlisted_payloads_without_network(
    monkeypatch: pytest.MonkeyPatch,
    hidden: bool,
) -> None:
    payloads: list[dict[str, object]] = []

    def capture(
        self: Client,
        method: str,
        pathname: str,
        *,
        request_kwargs: dict[str, object] | None = None,
        **kwargs: object,
    ) -> None:
        assert method == "POST" and pathname.endswith("/runs")
        assert request_kwargs is not None
        payloads.append(json.loads(bytes(request_kwargs["data"])))  # type: ignore[arg-type]

    monkeypatch.setattr(Client, "request_with_retries", capture)
    settings = Settings(
        langsmith_tracing=True,
        langsmith_api_key=SecretStr("dummy-local-key"),
        langsmith_agent_id="workcrew",
        langsmith_agent_environment="local",
        langsmith_hide_inputs=hidden,
        langsmith_hide_outputs=hidden,
    )
    adapter = build_trace_adapter(settings)
    assert adapter is not None
    async with (
        trace_session(adapter, organization_id=uuid4(), orchestration_run_id=uuid4()),
        trace_span("model") as span,
    ):
        span.model_reference = "mock:model-v1"
        span.input_tokens, span.output_tokens = 12, 4
    await adapter.close()
    assert len(payloads) == 2
    for payload in payloads:
        assert bool(payload["inputs"]) is not hidden
        assert bool(payload["outputs"]) is not hidden
        assert "dummy-local-key" not in json.dumps(payload)
        assert "session_name" not in payload
