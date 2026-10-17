"""Composition of optional hosted tracing with allowlisted operational I/O."""

import logging
from typing import cast

from langsmith import Client

from app.core.config import Settings
from work_management_ai.observability.langsmith_adapter import (
    LangSmithTraceAdapter,
    TraceClient,
    silence_sdk_transport_logs,
)

logger = logging.getLogger(__name__)


def build_trace_adapter(settings: Settings) -> LangSmithTraceAdapter | None:
    if not settings.langsmith_tracing or settings.langsmith_api_key is None:
        return None
    silence_sdk_transport_logs()
    try:
        client = Client(
            api_url=settings.langsmith_endpoint,
            api_key=settings.langsmith_api_key.get_secret_value(),
            auto_batch_tracing=False,
            hide_inputs=settings.langsmith_hide_inputs,
            hide_outputs=settings.langsmith_hide_outputs,
            omit_traced_runtime_info=True,
            disable_prompt_cache=True,
            timeout_ms=1000,
        )
        return LangSmithTraceAdapter(
            client=cast(TraceClient, client),
            hide_inputs=settings.langsmith_hide_inputs,
            hide_outputs=settings.langsmith_hide_outputs,
            address=f"lrn:agents/{settings.langsmith_agent_id}/environments/{settings.langsmith_agent_environment}",
        )
    except Exception:
        logger.warning("LANGSMITH_EXPORT_INITIALIZATION_FAILED")
        return None
