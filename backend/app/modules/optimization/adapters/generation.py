"""Resolve current actor and generation in one tenant-scoped snapshot query."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.optimization.application.cache_contracts import CacheRevision
from app.modules.organization.domain.roles import MembershipRole


async def read_actor_revision(
    session: AsyncSession,
    actor: AuthenticatedActor,
) -> tuple[AuthenticatedActor, CacheRevision | None] | None:
    row = (
        (
            await session.execute(
                text(
                    "SELECT m.user_id, m.role, u.email_display, u.display_name, "
                    "o.name AS organization_name, "
                    "g.epoch, g.generation FROM memberships m JOIN users u ON u.id=m.user_id "
                    "JOIN organizations o ON o.id=m.organization_id "
                    "LEFT JOIN read_model_generations g ON g.organization_id=m.organization_id "
                    "WHERE m.organization_id=:org AND m.id=:member AND m.user_id=:user "
                    "AND m.is_active AND u.is_active"
                ),
                {
                    "org": actor.organization_id,
                    "member": actor.membership_id,
                    "user": actor.user_id,
                },
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    current = AuthenticatedActor(
        user_id=row["user_id"],
        email=row["email_display"],
        display_name=row["display_name"],
        membership_id=actor.membership_id,
        organization_id=actor.organization_id,
        organization_name=row["organization_name"],
        role=MembershipRole(row["role"]),
    )
    revision = (
        None
        if row["epoch"] is None
        else CacheRevision(epoch=row["epoch"], generation=row["generation"])
    )
    return current, revision
