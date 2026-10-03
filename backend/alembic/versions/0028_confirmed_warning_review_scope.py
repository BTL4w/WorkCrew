"""Allow active Managers to review confirmed evidence warnings; drafts stay private."""

from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE POLICY confirmed_warning_manager_review ON warning_acknowledgments
        FOR SELECT TO app_runtime USING (
            organization_id=nullif(current_setting('app.organization_id',true),'')::uuid
            AND EXISTS (
                SELECT 1 FROM memberships m JOIN users u ON u.id=m.user_id
                WHERE m.organization_id=warning_acknowledgments.organization_id
                AND m.id=nullif(current_setting('app.membership_id',true),'')::uuid
                AND m.role IN ('MANAGER','ADMIN') AND m.is_active AND u.is_active
            )
        )
    """)


def downgrade() -> None:
    op.execute("DROP POLICY confirmed_warning_manager_review ON warning_acknowledgments")
