"""Version 1 prompts for bounded Work question answering."""

import json

from work_management_ai.agents.work_intelligence.contracts import (
    EvidenceItem,
    WorkIntelligenceInput,
    WorkQuestionKind,
)
from work_management_ai.model_gateway.contracts import ModelMessage
from work_management_ai.runtime.contracts import ContextReference

PROMPT_VERSION = "work-intelligence-system-v1"

_SYSTEM = """You are the read-only Work Intelligence Agent.
Select exactly one permitted read Tool or request a handoff back to the Orchestrator.
Never invent Tools, tenant scope, permissions, facts, writes, approvals or hidden reasoning.
For progress/evidence/risk questions, use risk.read with task_reference
and expected_fingerprint null,
or request risk.explain through the Orchestrator for a detailed explanation.
Planning intent must return a requested handoff for planning.create; do not call Planning.
"""


def build_step_plan_messages(
    value: WorkIntelligenceInput,
    *,
    skill_catalog: tuple[tuple[str, str], ...],
    context_references: tuple[ContextReference, ...],
) -> tuple[ModelMessage, ...]:
    payload = {
        "question": value.model_dump(mode="json"),
        "available_skills": [
            {"reference": reference, "description": description}
            for reference, description in skill_catalog
        ],
        "context_references": [item.model_dump(mode="json") for item in context_references],
    }
    return (
        ModelMessage(role="system", content=_SYSTEM),
        ModelMessage(role="user", content=json.dumps(payload, ensure_ascii=False, sort_keys=True)),
    )


def build_answer_messages(
    value: WorkIntelligenceInput,
    *,
    question_kind: WorkQuestionKind,
    evidence: tuple[EvidenceItem, ...],
    skill_instructions: str,
) -> tuple[ModelMessage, ...]:
    payload = {
        "question": value.model_dump(mode="json"),
        "question_kind": question_kind.value,
        "evidence": [item.model_dump(mode="json") for item in evidence],
        "skill_instructions": skill_instructions,
    }
    return (
        ModelMessage(
            role="system",
            content=(
                "Return only claims supported by supplied evidence. Every claim must include "
                "evidence IDs and exact field/value assertions. Do not expose hidden reasoning."
            ),
        ),
        ModelMessage(role="user", content=json.dumps(payload, ensure_ascii=False, sort_keys=True)),
    )
