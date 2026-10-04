"""The existing worker leases one bounded in-app delivery per tick."""

from ..application.digest_service import DigestService
from ..domain.digests import AuthorizedJobScope


class JobRunner:
    def __init__(self, service: DigestService):
        self.service = service

    async def run_once(self, scope: AuthorizedJobScope, worker_id: str) -> bool:
        return await self.service.deliver(scope, worker_id=worker_id)
