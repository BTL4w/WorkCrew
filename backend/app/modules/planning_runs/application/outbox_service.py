"""Service for processing transactional outbox events."""

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

from app.modules.planning_runs.application.job_service import compute_backoff_seconds
from app.modules.planning_runs.application.ports import PlanningRunTransaction
from app.modules.planning_runs.domain.models import OutboxEvent, PlanningRunDomainError

logger = logging.getLogger(__name__)


class OutboxPublisher(Protocol):
    """Protocol for publishing domain events."""

    async def publish(self, event: OutboxEvent) -> None:
        """Publish the event to external systems.

        Args:
            event: The event to publish.

        Raises:
            Exception: If publishing fails.
        """
        ...


class UnsupportedOutboxPublisher:
    """Production fallback publisher for unimplemented events.

    Logs and raises to fail safely. Real dispatch handlers will be added in Task 8.
    """

    async def publish(self, event: OutboxEvent) -> None:
        logger.error(
            "No outbox publisher implemented for event %s type %s", event.id, event.event_type
        )
        raise NotImplementedError(f"Publisher not implemented for {event.event_type}")


class OutboxService:
    """Application service for dispatching pending outbox events."""

    def __init__(
        self,
        *,
        transaction_factory: Callable[[UUID], PlanningRunTransaction],
        publisher: OutboxPublisher,
        organization_scopes: set[UUID],
        lease_seconds: int = 60,
    ) -> None:
        self._transaction_factory = transaction_factory
        self._publisher = publisher
        self._organization_scopes = organization_scopes
        self._lease_seconds = lease_seconds

    @property
    def lease_seconds(self) -> int:
        """The configured lease duration in seconds."""
        return self._lease_seconds

    async def dispatch_once(self, worker_id: str, organization_id: UUID) -> bool:
        """Attempt to claim and dispatch pending outbox events for a tenant.

        Args:
            worker_id: The identity of the worker process.
            organization_id: The tenant scope to operate within.

        Returns:
            True if any events were processed, False otherwise.

        Raises:
            PlanningRunDomainError: If organization_id is outside allowed scopes.
        """
        if organization_id not in self._organization_scopes:
            raise PlanningRunDomainError(f"Worker organization scope violation: {organization_id}")

        now = datetime.now(UTC)
        lease_until = now + timedelta(seconds=self._lease_seconds)

        async with self._transaction_factory(organization_id) as txn:
            events = await txn.repository.claim_pending_outbox_events(
                organization_id=organization_id,
                worker_id=worker_id,
                limit=50,
                now=now,
                lease_until=lease_until,
            )
            if not events:
                return False
            await txn.commit()

        # Process each event independently
        for event in events:
            try:
                await self._publisher.publish(event)
                async with self._transaction_factory(organization_id) as txn:
                    await txn.repository.mark_outbox_event_published(
                        organization_id=organization_id,
                        event_id=event.event_id,
                        worker_id=worker_id,
                        now=now,
                        published_at=datetime.now(UTC),
                    )
                    await txn.commit()
            except Exception as exc:
                safe_msg = str(exc)[:1000] or exc.__class__.__name__
                logger.warning(
                    "Failed to publish outbox event %s (attempt %d/%d): %s",
                    event.id,
                    event.attempt_count,
                    event.max_attempts,
                    safe_msg,
                )
                async with self._transaction_factory(organization_id) as txn:
                    backoff = compute_backoff_seconds(event.attempt_count)
                    await txn.repository.record_outbox_event_failure(
                        organization_id=organization_id,
                        event_id=event.event_id,
                        worker_id=worker_id,
                        now=now,
                        error_code="PUBLISH_ERROR",
                        error_message=safe_msg,
                        next_available_at=now + timedelta(seconds=backoff),
                    )
                    await txn.commit()

        return True
