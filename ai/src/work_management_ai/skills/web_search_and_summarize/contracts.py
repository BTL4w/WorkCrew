"""The skill consumes the same bounded contracts as its owning Specialist."""

from work_management_ai.agents.retrieve.contracts import (
    RetrieveInput as WebSearchAndSummarizeInput,
)
from work_management_ai.agents.retrieve.contracts import (
    RetrieveOutput as WebSearchAndSummarizeOutput,
)

__all__ = ["WebSearchAndSummarizeInput", "WebSearchAndSummarizeOutput"]
