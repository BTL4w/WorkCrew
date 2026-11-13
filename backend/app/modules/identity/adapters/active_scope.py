"""Current membership/user authority in the same SQL statement as streamed data."""

from sqlalchemy import select
from sqlalchemy.sql.selectable import Exists

from app.modules.identity.adapters.database_models import UserModel
from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.organization.adapters.database_models import MembershipModel
from app.modules.organization.domain.roles import MembershipRole


def active_actor_scope(actor: AuthenticatedActor, *, manager: bool = False) -> Exists:
    query = (
        select(MembershipModel.id)
        .join(UserModel, UserModel.id == MembershipModel.user_id)
        .where(
            MembershipModel.organization_id == actor.organization_id,
            MembershipModel.id == actor.membership_id,
            MembershipModel.is_active.is_(True),
            UserModel.is_active.is_(True),
        )
    )
    if manager:
        query = query.where(
            MembershipModel.role.in_((MembershipRole.ADMIN, MembershipRole.MANAGER))
        )
    return query.exists()
