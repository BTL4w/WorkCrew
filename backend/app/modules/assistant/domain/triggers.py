"""Typed execution intents share the provider-free runtime wire contract."""

from work_management_ai.runtime.triggers import (
    ChatTurnTrigger,
    ExecutionScope,
    ExecutionTrigger,
    NonChatTrigger,
    ReportRequestTrigger,
    SummaryJobTrigger,
)

__all__ = [
    "ChatTurnTrigger",
    "ExecutionScope",
    "ExecutionTrigger",
    "NonChatTrigger",
    "ReportRequestTrigger",
    "SummaryJobTrigger",
]


class TriggerError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)
