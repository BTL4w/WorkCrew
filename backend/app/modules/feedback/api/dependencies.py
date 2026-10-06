from typing import Annotated, cast

from fastapi import Depends, Request

from ..application.feedback_service import FeedbackService


def get_feedback_service(request: Request) -> FeedbackService:
    return cast(FeedbackService, request.app.state.feedback_service)


FeedbackServiceDependency = Annotated[FeedbackService, Depends(get_feedback_service)]
