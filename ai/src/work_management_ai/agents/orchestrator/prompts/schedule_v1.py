"""Versioned preview-only schedule interpretation; authority stays in application code."""

from work_management_ai.model_gateway.contracts import ModelMessage

PROMPT_VERSION = "orchestrator-schedule-v1"
_SYSTEM = """Extract only the explicit daily-summary schedule request.
No tenant, role, approval or business write.
Return exact Project/person references from the message; use SELF for the requester.
Leave unspecified config fields null. Never invent UUIDs.
CONFIGURE creates only a preview. PAUSE/RESUME also require human confirmation.
Weekdays are Monday=1 through Sunday=7. Use IANA timezone.
"""


def build_schedule_messages(message: str) -> tuple[ModelMessage, ...]:
    return (
        ModelMessage(role="system", content=_SYSTEM),
        ModelMessage(role="user", content=message),
    )
