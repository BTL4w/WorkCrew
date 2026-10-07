from work_management_ai.evaluation.core_mvp import evaluate_core_gate


def test_core_gate_requires_each_original_suite_and_reporting():
    assert not evaluate_core_gate({})
    assert not evaluate_core_gate({"phase5_reporting": True})


def test_existing_suite_failure_cannot_be_hidden_by_reporting():
    suites = {
        name: True
        for name in (
            "phase2_multi_agent",
            "phase3_assignment",
            "phase4_daily_update",
            "phase4_risk",
            "phase4_daily_update_risk",
            "phase5_reporting",
        )
    }
    assert evaluate_core_gate(suites)
    suites["phase4_risk"] = False
    assert not evaluate_core_gate(suites)
