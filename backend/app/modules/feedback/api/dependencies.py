from typing import Annotated, cast

from fastapi import Depends, Request

from ..application.feedback_service import FeedbackService
from ..application.outcome_service import OutcomeService


def get_feedback_service(request: Request) -> FeedbackService:
    return cast(FeedbackService, request.app.state.feedback_service)


FeedbackServiceDependency = Annotated[FeedbackService, Depends(get_feedback_service)]


def get_outcome_service(request: Request) -> OutcomeService:
    return cast(OutcomeService, request.app.state.feedback_outcome_service)


OutcomeServiceDependency = Annotated[OutcomeService, Depends(get_outcome_service)]
