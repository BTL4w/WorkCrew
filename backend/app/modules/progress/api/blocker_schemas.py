"""Public typed lifecycle mutation contract."""

from app.modules.progress.domain.blockers import BlockerCommand


class BlockerMutationRequest(BlockerCommand):
    pass
