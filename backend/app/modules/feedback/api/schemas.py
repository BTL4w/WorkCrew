from ..domain.feedback import FeedbackCommand, FeedbackResult
from ..domain.outcomes import FeedbackOutcome, OutcomeSourceCommand


class FeedbackRequest(FeedbackCommand):
    pass


class FeedbackResponse(FeedbackResult):
    pass


class OutcomeRequest(OutcomeSourceCommand):
    pass


class OutcomeResponse(FeedbackOutcome):
    pass
