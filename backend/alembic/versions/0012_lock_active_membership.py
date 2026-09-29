"""Add a tenant-scoped membership reference lock.

Revision ID: 0012
Revises: 0011
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0012"
down_revision: str | Sequence[str] | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Allow app_runtime to lock an active membership without UPDATE authority."""

    op.execute(
        """
        CREATE FUNCTION public.lock_active_membership(
            p_organization_id uuid,
            p_membership_id uuid
        ) RETURNS boolean
        LANGUAGE sql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $function$
            SELECT COALESCE(
                (
                    SELECT membership.is_active
                    FROM public.memberships AS membership
                    WHERE membership.organization_id = p_organization_id
                      AND membership.id = p_membership_id
                      AND membership.organization_id = NULLIF(
                          current_setting('app.organization_id', true), ''
                      )::uuid
                    FOR NO KEY UPDATE
                ),
                false
            )
        $function$
        """
    )
    op.execute("ALTER FUNCTION public.lock_active_membership(uuid, uuid) OWNER TO migration_owner")
    op.execute("REVOKE ALL ON FUNCTION public.lock_active_membership(uuid, uuid) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION public.lock_active_membership(uuid, uuid) TO app_runtime")


def downgrade() -> None:
    """Remove the tenant-scoped membership reference lock."""

    op.execute("DROP FUNCTION public.lock_active_membership(uuid, uuid)")
