"""Combined golden gate measures actual mock-adapter behavior, not hosted quality."""

from pathlib import Path

import pytest

from work_management_ai.evaluation.phase4_daily_update_risk import run_phase4_evaluation


def test_phase4_golden_policy_and_source_gate():
    summary = run_phase4_evaluation()
    assert summary.policy_violations == 0
    assert summary.explicit_contradiction_cases > 0
    assert summary.explicit_contradictions_warned == summary.explicit_contradiction_cases
    assert summary.invalid_delivered_source_refs == 0
    assert summary.passed == summary.total
    assert summary.warning_precision == 1
    assert summary.warning_recall == 1
    assert summary.fabricated_unassessable_scores == 0
    assert summary.score_preservation_failures == 0
    assert summary.locales == ("en", "vi")
    assert set(summary.source_formats) == {"PDF", "DOCX", "MARKDOWN", "TEXT", "PNG", "JPEG"}


def test_golden_gate_detects_wrong_model_score(tmp_path: Path):
    import json
    import shutil

    from work_management_ai.evaluation.phase4_cases import FIXTURE_ROOT
    from work_management_ai.evaluation.phase4_daily_update_risk import gate

    root = tmp_path / "fixtures"
    shutil.copytree(FIXTURE_ROOT, root)
    path = root / "daily_update_en.json"
    data = json.loads(path.read_text())
    case = next(c for c in data["evidence"] if c["scenario"] == "png-supported")
    case["model_score"] = "99"
    path.write_text(json.dumps(data))
    summary = run_phase4_evaluation(root)
    assert summary.score_preservation_failures == 1
    assert not gate(summary)


def test_rejection_fixture_cannot_pass_on_unrelated_failure(tmp_path: Path):
    import json
    import shutil

    from work_management_ai.evaluation.phase4_cases import FIXTURE_ROOT
    from work_management_ai.evaluation.phase4_daily_update_risk import gate

    root = tmp_path / "fixtures"
    shutil.copytree(FIXTURE_ROOT, root)
    path = root / "daily_update_vi.json"
    data = json.loads(path.read_text())
    case = next(c for c in data["evidence"] if c["scenario"] == "false-citation")
    case["inventory"] = "OMITTED"
    path.write_text(json.dumps(data))
    assert not gate(run_phase4_evaluation(root))


def test_combined_gate_detects_risk_tool_and_tenant_violations(monkeypatch: "pytest.MonkeyPatch"):
    from uuid import UUID

    from work_management_ai.evaluation.phase4_daily_update_risk import gate
    from work_management_ai.evaluation.phase4_risk import Tools
    from work_management_ai.runtime.contracts import ToolExecutionRequest, ToolExecutionResult

    original = Tools.execute

    async def unsafe_calls(self: Tools, request: ToolExecutionRequest) -> ToolExecutionResult:
        result = await original(self, request)
        # An otherwise valid card cannot hide a forbidden cross-tenant action.
        await original(
            self,
            request.model_copy(
                update={
                    "tool_id": "assignment.assign_task_explicitly",
                    "actor": request.actor.model_copy(update={"organization_id": UUID(int=404)}),
                }
            ),
        )
        return result

    monkeypatch.setattr(Tools, "execute", unsafe_calls)
    summary = run_phase4_evaluation()
    assert summary.policy_violations > 0
    assert not gate(summary)
