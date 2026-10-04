from typing import cast

from fastapi import Request

from ..application.schedule_service import ScheduleService


def get_schedule_service(request: Request) -> ScheduleService:
    return cast(ScheduleService, request.app.state.schedule_service)
