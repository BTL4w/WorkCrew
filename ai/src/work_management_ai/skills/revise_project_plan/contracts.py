"""Revision Planning Skill aliases."""

from work_management_ai.agents.planning.contracts import PlanningAgentInput, PlanningAgentOutput
from work_management_ai.agents.risk.contracts import RiskReplanRequest

RiskRevisionContext = RiskReplanRequest

ReviseProjectPlanInput = PlanningAgentInput
ReviseProjectPlanOutput = PlanningAgentOutput

__all__ = ["ReviseProjectPlanInput", "ReviseProjectPlanOutput", "RiskRevisionContext"]
