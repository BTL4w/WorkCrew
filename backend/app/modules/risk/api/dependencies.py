"""Inject application services; authority always comes from current membership checks."""

from typing import cast

from fastapi import Request

from app.modules.risk.application.risk_service import RiskService


def get_risk_service(request: Request) -> RiskService:
    return cast(RiskService, request.app.state.risk_service)
