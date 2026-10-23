"""Allowlisted controls for the dedicated synthetic browser database; no API surface."""

from __future__ import annotations

import json
import os
import sys
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from sqlalchemy import create_engine, text


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: Literal["inspect", "stop", "revoke", "restore"]
    conversation_id: UUID


def main() -> None:
    url = os.environ["RETRIEVAL_TEST_DATABASE_URL"]
    parsed = urlsplit(url)
    if (
        parsed.hostname not in {"localhost", "127.0.0.1"}
        or parsed.port != 55432
        or parsed.path != "/work_management_retrieve_task6_e2e"
    ):
        raise ValueError("DEDICATED_SYNTHETIC_DATABASE_REQUIRED")
    organization = UUID(os.environ["RETRIEVAL_TEST_ORGANIZATION_ID"])
    command = Command.model_validate_json(sys.stdin.read())
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(
            text("SELECT set_config('app.organization_id', :org, true)"), {"org": str(organization)}
        )
        params = {"org": organization, "conversation": command.conversation_id}
        # Resolve the fixture owner from a tenant-scoped conversation, never arbitrary users.
        owner = conn.execute(
            text("""SELECT c.owner_membership_id FROM assistant_conversations c
            JOIN memberships m ON m.organization_id=c.organization_id AND m.id=c.owner_membership_id
            JOIN users u ON u.id=m.user_id
            WHERE c.organization_id=:org AND c.id=:conversation AND
                u.email_normalized='manager@example.test'"""),
            params,
        ).scalar_one()
        if command.operation in {"revoke", "restore"}:
            conn.execute(
                text(
                    """UPDATE memberships SET is_active=:active WHERE organization_id=:org
                    AND id=:owner"""
                ),
                {"org": organization, "owner": owner, "active": command.operation == "restore"},
            )
        if command.operation == "stop":
            conn.execute(
                text("""UPDATE assistant_jobs SET status='FAILED', locked_by=NULL, lease_until=NULL,
                safe_error_code='RETRIEVAL_CANCELLED' WHERE organization_id=:org AND
                conversation_id=:conversation
                AND job_type='assistant.turn.execute' AND status='RUNNING'"""),
                params,
            )
        result: dict[str, int] = {}
        for label, sql in {
            "dispatched": """SELECT count(*) FROM retrieval_attempt_reservations a
                JOIN agent_runs r ON
                r.organization_id=a.organization_id AND r.id=a.agent_run_id JOIN
                orchestration_runs o ON o.organization_id=r.organization_id AND
                o.id=r.orchestration_run_id JOIN assistant_turns t
                ON t.organization_id=o.organization_id AND t.id=o.turn_id
                WHERE a.organization_id=:org AND
                t.conversation_id=:conversation AND a.kind='SEARCH' AND a.dispatched""",
            "sources": """SELECT count(*) FROM retrieval_source_snapshots s JOIN agent_runs r ON
                r.organization_id=s.organization_id AND r.id=s.agent_run_id JOIN
                orchestration_runs o ON o.organization_id=r.organization_id AND
                o.id=r.orchestration_run_id JOIN assistant_turns t
                ON t.organization_id=o.organization_id AND t.id=o.turn_id
                WHERE s.organization_id=:org AND
                t.conversation_id=:conversation""",
            "bound_planning": """SELECT count(*) FROM agent_handoffs h
                JOIN orchestration_runs o ON o.organization_id=h.organization_id
                    AND o.id=h.orchestration_run_id
                JOIN assistant_turns t ON t.organization_id=o.organization_id AND t.id=o.turn_id
                JOIN agent_runs p ON p.organization_id=h.organization_id
                    AND p.id=h.parent_agent_run_id
                WHERE h.organization_id=:org AND t.conversation_id=:conversation
                    AND h.target_agent_id='planning' AND p.agent_id='orchestrator'
                    AND jsonb_array_length(h.typed_input->'retrieval_context_refs')>0""",
            "projects": "SELECT count(*) FROM projects WHERE organization_id=:org",
            "tasks": "SELECT count(*) FROM tasks WHERE organization_id=:org",
        }.items():
            result[label] = int(conn.execute(text(sql), params).scalar_one())
        print(json.dumps(result))
    engine.dispose()


if __name__ == "__main__":
    main()
