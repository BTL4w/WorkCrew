"""Planning Specialist step-selection prompt."""

import json

from work_management_ai.agents.planning.contracts import PlanningAgentInput
from work_management_ai.model_gateway.contracts import ModelMessage
from work_management_ai.runtime.contracts import ContextReference

PROMPT_VERSION = "planning-agent-system-v2"


def build_step_plan_messages(
    value: PlanningAgentInput,
    *,
    selected_skill: str,
    skill_instructions: str,
    skill_catalog: tuple[tuple[str, str], ...],
    context_references: tuple[ContextReference, ...],
) -> tuple[ModelMessage, ...]:
    payload = {
        "planning_input": value.model_dump(mode="json", exclude_none=True, exclude_defaults=True),
        "selected_skill": selected_skill,
        "selected_skill_instructions": skill_instructions,
        "skill_catalog": [
            {"reference": reference, "description": description}
            for reference, description in skill_catalog
        ],
        "context_references": [item.model_dump(mode="json") for item in context_references],
    }
    return (
        ModelMessage(
            role="system",
            content=(
                "Select only the declared Planning Skill and planning.manage_run Tool. "
                "Return assignment or other out-of-scope capability requests to the "
                "Orchestrator. Never approve, apply, persist directly, or invent authority."
            ),
        ),
        ModelMessage(role="user", content=json.dumps(payload, ensure_ascii=False, sort_keys=True)),
    )
