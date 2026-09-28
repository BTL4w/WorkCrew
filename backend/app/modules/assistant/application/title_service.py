"""Policy-authorized naming of user-owned operational metadata, outside Orchestrator."""

from app.modules.assistant.application.ports import AssistantTransactionFactory
from app.modules.assistant.domain.models import AssistantJob
from app.modules.identity.domain.auth import AuthenticatedActor
from work_management_ai.conversation_title import (
    TITLE_PROMPT_VERSION,
    TITLE_SCHEMA_VERSION,
    TITLE_VERIFIER_VERSION,
    generate_title,
)
from work_management_ai.model_gateway.contracts import ModelGateway


class ConversationTitleService:
    def __init__(
        self,
        *,
        transaction_factory: AssistantTransactionFactory,
        gateway: ModelGateway,
        timeout_seconds: float = 3,
    ) -> None:
        self._transactions = transaction_factory
        self._gateway = gateway
        self._timeout = timeout_seconds

    async def execute_job(self, *, job: AssistantJob, actor: AuthenticatedActor) -> None:
        async with self._transactions(actor) as txn:
            value = await txn.repository.get_conversation_title_input(actor=actor, job=job)
        if value is None:
            return
        result = await generate_title(
            self._gateway,
            message=value.message,
            locale=value.locale,
            timeout_seconds=self._timeout,
        )
        async with self._transactions(actor) as txn:
            await txn.repository.set_conversation_title(
                actor=actor,
                job=job,
                title=result.title,
                metadata={
                    "policy": "user_owned_conversation_title.v1",
                    "model_ref": result.model_ref,
                    "prompt_version": TITLE_PROMPT_VERSION,
                    "schema_version": TITLE_SCHEMA_VERSION,
                    "verifier_version": TITLE_VERIFIER_VERSION,
                    "fallback": result.fallback,
                    "safe_error_code": result.safe_error_code,
                },
            )
            await txn.commit()
