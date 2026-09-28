import asyncio
import os
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.modules.assistant.adapters.agent_runtime import build_agent_registry
from app.modules.planning_runs.adapters.ai_runtime import build_planning_job_handlers
from app.worker import process_tenant_once
from work_management_ai.runtime.contracts import AgentId


def test_worker_organization_ids_parsing() -> None:
    # Test valid JSON list of UUIDs
    os.environ["APP_WORKER_ORGANIZATION_IDS"] = (
        '["00000000-0000-0000-0000-000000000001", "00000000-0000-0000-0000-000000000002"]'
    )
    settings = Settings(environment="test")
    assert settings.worker_organization_ids == [
        UUID("00000000-0000-0000-0000-000000000001"),
        UUID("00000000-0000-0000-0000-000000000002"),
    ]

    # Test invalid UUID rejected
    os.environ["APP_WORKER_ORGANIZATION_IDS"] = '["not-a-uuid"]'
    with pytest.raises(ValidationError):
        Settings(environment="test")

    # Test empty JSON list
    os.environ["APP_WORKER_ORGANIZATION_IDS"] = "[]"
    settings = Settings(environment="test")
    assert settings.worker_organization_ids == []

    # Test empty string from environment (e.g. compose default unset)
    os.environ["APP_WORKER_ORGANIZATION_IDS"] = ""
    settings = Settings(environment="test")
    assert settings.worker_organization_ids == []

    # Clean up
    del os.environ["APP_WORKER_ORGANIZATION_IDS"]


def test_task_8_worker_registers_planning_and_finalization_handlers() -> None:
    handlers = build_planning_job_handlers(
        Settings(environment="test"),
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
    )

    assert set(handlers) == {
        "planning.start",
        "planning.resume",
        "planning.finalize",
        "proposal.ai_revise",
        "proposal.revalidate",
    }
    assert "approval.apply" not in handlers


def test_task_7_worker_registers_all_active_agent_manifests() -> None:
    registry, _ = build_agent_registry()

    assert registry.resolve(AgentId.ORCHESTRATOR, "1.0.0", 2)
    assert registry.resolve(AgentId.WORK_INTELLIGENCE, "1.0.0", 2)
    assert registry.resolve(AgentId.PLANNING, "1.0.0", 2)


@pytest.mark.asyncio
async def test_worker_iteration_is_bounded_and_fair() -> None:
    calls: list[str] = []

    class Outbox:
        async def dispatch_once(self, worker_id: str, organization_id: UUID) -> bool:
            calls.append("outbox")
            return True

    class Assistant:
        async def run_once(self, *, worker_id: str, organization_id: UUID) -> bool:
            calls.append("assistant")
            return True

    class Planning:
        async def run_once(self, worker_id: str, organization_id: UUID) -> bool:
            calls.append("planning")
            return True

    processed = await process_tenant_once(
        worker_id="worker",
        organization_id=UUID("00000000-0000-0000-0000-000000000001"),
        outbox_service=Outbox(),  # type: ignore[arg-type]
        assistant_job_service=Assistant(),  # type: ignore[arg-type]
        planning_job_service=Planning(),  # type: ignore[arg-type]
    )

    assert processed is True
    assert calls == ["outbox", "assistant", "planning"]


@pytest.mark.asyncio
async def test_naming_call_does_not_block_the_current_answer() -> None:
    title_started, answer_completed = asyncio.Event(), asyncio.Event()

    class Title:
        async def run_once(self, *, worker_id: str, organization_id: UUID) -> bool:
            title_started.set()
            await answer_completed.wait()
            return True

    class Assistant:
        async def run_once(self, *, worker_id: str, organization_id: UUID) -> bool:
            await title_started.wait()
            answer_completed.set()
            return True

    class Idle:
        async def dispatch_once(self, worker_id: str, organization_id: UUID) -> bool:
            return False

        async def run_once(self, worker_id: str, organization_id: UUID) -> bool:
            return False

    assert await asyncio.wait_for(
        process_tenant_once(
            worker_id="worker",
            organization_id=UUID("00000000-0000-0000-0000-000000000001"),
            outbox_service=Idle(),
            assistant_job_service=Assistant(),
            planning_job_service=Idle(),
            title_job_service=Title(),
        ),
        timeout=1,
    )
    assert answer_completed.is_set()


def test_title_model_defaults_to_4o_mini_and_accepts_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("APP_AI_TITLE_MODEL", raising=False)
    assert Settings.model_fields["ai_title_model"].default == "gpt-4o-mini"
    monkeypatch.setenv("APP_AI_TITLE_MODEL", "custom-title-model")
    assert Settings(environment="test").ai_title_model == "custom-title-model"
