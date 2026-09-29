import asyncio
from uuid import uuid4

from sqlalchemy import text

from app.core.config import Settings
from app.core.database import create_database_engine


async def main():
    settings = Settings()
    if not settings.database_url.endswith("/work_management_e2e"):
        raise RuntimeError("Browser fixtures require the dedicated E2E database")
    engine = create_database_engine(settings)
    async with engine.begin() as c:
        org, member, manager = (
            await c.execute(
                text(
                    "SELECT e.organization_id,e.id,m.id FROM memberships e "
                    "JOIN users u ON u.id=e.user_id JOIN memberships m "
                    "ON m.organization_id=e.organization_id AND m.role='MANAGER' "
                    "WHERE u.email_normalized='employee@example.test' LIMIT 1"
                )
            )
        ).one()
        project, task = uuid4(), uuid4()
        await c.execute(
            text(
                "INSERT INTO projects(id,organization_id,name,created_by_membership_id,"
                "updated_by_membership_id) "
                "VALUES (:id,:org,'Daily update browser fixture',:m,:m)"
            ),
            {"id": project, "org": org, "m": manager},
        )
        await c.execute(
            text(
                "INSERT INTO tasks(id,organization_id,project_id,title,status,"
                "required_skill_labels,assignee_membership_id,created_by_membership_id,"
                "updated_by_membership_id) "
                "VALUES (:id,:org,:p,:title,'IN_PROGRESS','[]',:m,:creator,:creator)"
            ),
            {
                "id": task,
                "org": org,
                "p": project,
                "m": member,
                "creator": manager,
                "title": f"Daily update {task}",
            },
        )
        print(task)
    await engine.dispose()


asyncio.run(main())
