"""Run each original Core MVP suite and Reporting once; preserve suite-specific semantics."""

import asyncio
import json
import subprocess
import sys
from typing import Any

from .phase5_cases import load_cases
from .phase5_reporting import MockReportingEvaluationGateway, run_reporting_suite

LEGACY_SUITES = (
    "phase2_multi_agent",
    "phase3_assignment",
    "phase4_daily_update",
    "phase4_risk",
    "phase4_daily_update_risk",
)


def evaluate_core_gate(suites: dict[str, bool]) -> bool:
    required = set(LEGACY_SUITES) | {"phase5_reporting"}
    return required.issubset(suites) and all(suites[name] for name in required)


def main() -> int:
    reports: dict[str, Any] = {}
    gates: dict[str, bool] = {}
    for name in LEGACY_SUITES:
        process = subprocess.run(
            [sys.executable, "-m", f"work_management_ai.evaluation.{name}"],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        try:
            reports[name] = json.loads(process.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            reports[name] = {"status": "UNMEASURED"}
        gates[name] = process.returncode == 0 and reports[name].get("status") != "UNMEASURED"
    reporting = asyncio.run(run_reporting_suite(load_cases(), MockReportingEvaluationGateway()))
    reports["phase5_reporting"] = reporting.model_dump(mode="json")
    gates["phase5_reporting"] = reporting.gate.passed
    passed = evaluate_core_gate(gates)
    print(
        json.dumps(
            {
                "passed": passed,
                "suites": reports,
                "gates": gates,
                "limitations": [
                    "PHASE4_COMBINED_OVERLAPS_DAILY_AND_RISK_COUNTS",
                    "MOCK_PASS_IS_NOT_HOSTED_ACCURACY",
                ],
            },
            sort_keys=True,
        )
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
