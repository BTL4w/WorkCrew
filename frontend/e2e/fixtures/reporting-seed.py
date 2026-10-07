"""Only seed Manager-owned work in the disposable browser DB; risk remains worker-owned."""

import asyncio
import json
import sys
from uuid import UUID, uuid4

from sqlalchemy import text

from app.core.config import Settings
from app.core.database import create_database_engine


async def main():
    settings = Settings()
    if not settings.database_url.endswith("/work_management_e2e"):
        raise RuntimeError("Browser fixture requires the dedicated E2E database")
    engine = create_database_engine(settings)
    try:
        if sys.argv[1] == "--employee":
            async with engine.connect() as c:
                email = await c.scalar(
                    text(
                        "SELECT u.email_normalized FROM memberships m JOIN users u ON "
                        "u.id=m.user_id "
                        "WHERE m.id=:id AND m.role='EMPLOYEE' AND u.email_normalized LIKE "
                        "'%@example.test'"
                    ),
                    {"id": UUID(sys.argv[2])},
                )
            assert email is not None
            print(email)
            return
        if sys.argv[1] == "--conversation":
            conversation = UUID(sys.argv[2])
            async with engine.connect() as c:
                rows = (
                    await c.execute(
                        text(
                            "SELECT "
                            "h.target_agent_id,p.agent_id,h.organization_id,p.organization_id,"
                            "r.organization_id,r.actor_membership_id,m.id FROM agent_handoffs h "
                            "JOIN agent_runs p ON p.id=h.parent_agent_run_id "
                            "JOIN orchestration_runs r ON r.id=h.orchestration_run_id "
                            "JOIN assistant_turns t ON t.id=r.turn_id "
                            "AND t.organization_id=r.organization_id "
                            "JOIN memberships m ON m.organization_id=r.organization_id "
                            "JOIN users u ON u.id=m.user_id WHERE t.conversation_id=:id "
                            "AND u.email_normalized='manager@example.test'"
                        ),
                        {"id": conversation},
                    )
                ).all()
            assert len(rows) >= 4
            targets = {row[0] for row in rows}
            assert {"planning", "daily_update", "reporting"} <= targets
            assert all(
                row[1] == "orchestrator" and row[2] == row[3] == row[4] and row[5] == row[6]
                for row in rows
            )
            print(
                json.dumps(
                    {
                        "handoffs": len(rows),
                        "specialists": sorted(targets),
                        "peer_handoff_count": 0,
                        "cross_tenant_leakage_count": 0,
                    }
                )
            )
            return
        project = UUID(sys.argv[1])
        async with engine.begin() as c:
            org, member, week = (
                await c.execute(
                    text(
                        "SELECT p.organization_id,m.id,w.id FROM projects p "
                        "JOIN memberships m ON m.organization_id=p.organization_id "
                        "JOIN users u ON u.id=m.user_id JOIN project_weeks w ON w.project_id=p.id "
                        "WHERE p.id=:id AND u.email_normalized='manager@example.test' ORDER BY "
                        "w.week_number LIMIT 1"
                    ),
                    {"id": project},
                )
            ).one()
            task = uuid4()
            title = f"Manager owned survey {task}"
            await c.execute(
                text(
                    "INSERT INTO tasks(id,organization_id,project_id,project_week_id,title,status,"
                    "required_skill_labels,assignee_membership_id,estimated_effort_hours,"
                    "created_by_membership_id,updated_by_membership_id) "
                    "VALUES "
                    "(:id,:org,:project,:week,:title,'IN_PROGRESS','[]',:member,2,:member,:memb"
                    "er)"
                ),
                {
                    "id": task,
                    "org": org,
                    "project": project,
                    "week": week,
                    "title": title,
                    "member": member,
                },
            )
            print(json.dumps({"id": str(task), "title": title}))
    finally:
        await engine.dispose()


asyncio.run(main())
