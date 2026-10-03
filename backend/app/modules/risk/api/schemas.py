from uuid import UUID

from app.modules.risk.domain.assessments import Contract


class RiskRefreshRequest(Contract):
    task_id: UUID
