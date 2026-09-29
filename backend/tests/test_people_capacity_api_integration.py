"""ASGI contract tests for People Skills endpoints."""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pwdlib import PasswordHash
from sqlalchemy import text

from app.core.config import Settings
from app.core.database import create_database_engine
from app.main import create_app
from app.modules.identity.api.dependencies import get_authenticated_actor
from app.modules.identity.domain.auth import AuthenticatedActor
from app.modules.organization.domain.roles import MembershipRole
from app.modules.people_capacity.application.ports import PeopleMutationResult
from app.modules.people_capacity.domain.skills import (
    PeopleSkillForbiddenError,
    PeopleSkillReferenceError,
    Skill,
    SkillLevel,
    VerifiedPersonSkill,
)


def _actor() -> AuthenticatedActor:
    return AuthenticatedActor(
        user_id=uuid4(),
        email="manager@example.test",
        display_name="Manager",
        membership_id=uuid4(),
        organization_id=uuid4(),
        organization_name="Tenant",
        role=MembershipRole.MANAGER,
    )


class StubPeopleCapacityService:
    def __init__(self, actor: AuthenticatedActor) -> None:
        now = datetime(2026, 8, 26, tzinfo=UTC)
        self.skill = Skill(
            id=uuid4(),
            organization_id=actor.organization_id,
            name="Facilitation",
            normalized_name="facilitation",
            description=None,
            active=True,
            version=1,
            created_at=now,
            updated_at=now,
        )
        self.person_skill = VerifiedPersonSkill(
            id=uuid4(),
            organization_id=actor.organization_id,
            membership_id=uuid4(),
            skill_id=self.skill.id,
            level=SkillLevel.LEVEL_4,
            verified_by_membership_id=actor.membership_id,
            verified_at=now,
            version=2,
            created_at=now,
            updated_at=now,
        )
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail_transport_audit = False

    async def authorize_mutation(self, **values: Any) -> None:
        self.calls.append(("authorize_mutation", values))
        if values["actor"].role is MembershipRole.EMPLOYEE:
            raise PeopleSkillForbiddenError

    async def audit_transport_rejection(self, **values: Any) -> None:
        self.calls.append(("audit_transport_rejection", values))
        if self.fail_transport_audit:
            raise RuntimeError("audit unavailable")

    async def list_skills(self, **values: Any):  # type: ignore[no-untyped-def]
        self.calls.append(("list_skills", values))
        return (self.skill,)

    async def create_skill(self, **values: Any):  # type: ignore[no-untyped-def]
        self.calls.append(("create_skill", values))
        return PeopleMutationResult(self.skill, False)

    async def set_person_skill(self, **values: Any):  # type: ignore[no-untyped-def]
        self.calls.append(("set_person_skill", values))
        if any(
            item.evidence_type.value == "COMPLETED_TASK" and item.source_resource_type != "task"
            for item in values["evidence"]
        ):
            raise PeopleSkillReferenceError("source_resource_type")
        return PeopleMutationResult(self.person_skill, True)

    async def get_skill(self, **values: Any):  # type: ignore[no-untyped-def]
        self.calls.append(("get_skill", values))
        return self.skill

    async def update_skill(self, **values: Any):  # type: ignore[no-untyped-def]
        self.calls.append(("update_skill", values))
        return PeopleMutationResult(self.skill, False)

    async def list_person_skills(self, **values: Any):  # type: ignore[no-untyped-def]
        self.calls.append(("list_person_skills", values))
        return (self.person_skill,)

    async def get_person_skill(self, **values: Any):  # type: ignore[no-untyped-def]
        self.calls.append(("get_person_skill", values))
        return self.person_skill

    async def delete_person_skill(self, **values: Any):  # type: ignore[no-untyped-def]
        self.calls.append(("delete_person_skill", values))
        return PeopleMutationResult(self.person_skill, False)

    async def list_work_outcome_evidence(self, **values: Any):  # type: ignore[no-untyped-def]
        self.calls.append(("list_work_outcome_evidence", values))
        return ()

    async def list_skill_evidence(self, **values: Any):  # type: ignore[no-untyped-def]
        self.calls.append(("list_skill_evidence", values))
        return ()


def _app(actor: AuthenticatedActor, service: StubPeopleCapacityService) -> FastAPI:
    app = create_app(Settings(environment="test"), people_capacity_service=service)  # type: ignore[arg-type]
    app.dependency_overrides[get_authenticated_actor] = lambda: actor
    return app


@pytest.mark.asyncio
async def test_skill_create_and_list_contracts() -> None:
    actor, service = _actor(), None
    service = StubPeopleCapacityService(actor)
    async with AsyncClient(
        transport=ASGITransport(app=_app(actor, service)), base_url="http://testserver"
    ) as client:
        created = await client.post(
            "/api/v1/skills",
            json={"name": " Facilitation ", "description": None},
            headers={"Idempotency-Key": "skill-create-key1"},
        )
        listed = await client.get("/api/v1/skills")
        invalid = await client.post(
            "/api/v1/skills",
            json={"name": "X", "unexpected": True},
            headers={"Idempotency-Key": "skill-create-key2"},
        )

    assert created.status_code == 201
    assert created.headers["etag"] == '"1"'
    assert created.json()["normalized_name"] == "facilitation"
    assert listed.json() == [created.json()]
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "VALIDATION_FAILED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "body", "headers", "expected_status"),
    [
        (
            "POST",
            "/api/v1/skills",
            {"name": "Facilitation", "unexpected": True},
            {"Idempotency-Key": "invalid-body-key1"},
            422,
        ),
        (
            "POST",
            "/api/v1/skills",
            {"name": "Facilitation"},
            {},
            422,
        ),
        (
            "PATCH",
            "/api/v1/skills/{skill_id}",
            {"description": "Updated"},
            {"Idempotency-Key": "invalid-etag-key1", "If-Match": "invalid"},
            400,
        ),
    ],
)
async def test_writer_transport_rejections_are_audited(
    method: str,
    path: str,
    body: dict[str, object],
    headers: dict[str, str],
    expected_status: int,
) -> None:
    actor = _actor()
    service = StubPeopleCapacityService(actor)
    resolved_path = path.format(skill_id=service.skill.id)
    async with AsyncClient(
        transport=ASGITransport(app=_app(actor, service)), base_url="http://testserver"
    ) as client:
        response = await client.request(method, resolved_path, json=body, headers=headers)

    assert response.status_code == expected_status
    audit = next(values for name, values in service.calls if name == "audit_transport_rejection")
    assert audit["actor"] == actor
    assert audit["reason_code"] in {"VALIDATION_FAILED", "INVALID_REQUEST"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("role", "expected_status", "expected_code"),
    [
        (MembershipRole.MANAGER, 422, "VALIDATION_FAILED"),
        (MembershipRole.EMPLOYEE, 403, "FORBIDDEN"),
    ],
)
async def test_malformed_json_is_authorized_before_validation_and_audited(
    role: MembershipRole, expected_status: int, expected_code: str
) -> None:
    actor = _actor()
    actor = replace(actor, role=role)
    service = StubPeopleCapacityService(actor)
    async with AsyncClient(
        transport=ASGITransport(app=_app(actor, service)), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/v1/skills",
            content="{",
            headers={"Content-Type": "application/json", "Idempotency-Key": "malformed-json-1"},
        )

    assert response.status_code == expected_status
    assert response.json()["error"]["code"] == expected_code
    assert any(name == "authorize_mutation" for name, _ in service.calls)
    if role is MembershipRole.MANAGER:
        assert any(name == "audit_transport_rejection" for name, _ in service.calls)


@pytest.mark.asyncio
async def test_oversized_idempotency_key_does_not_break_rejection_audit() -> None:
    actor = _actor()
    service = StubPeopleCapacityService(actor)
    async with AsyncClient(
        transport=ASGITransport(app=_app(actor, service)), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/api/v1/skills",
            json={"name": "Facilitation"},
            headers={"Idempotency-Key": "x" * 129},
        )

    assert response.status_code == 422
    audit = next(values for name, values in service.calls if name == "audit_transport_rejection")
    assert audit["idempotency_key"] is None


@pytest.mark.asyncio
async def test_transport_rejection_fails_closed_when_audit_is_unavailable() -> None:
    actor = _actor()
    service = StubPeopleCapacityService(actor)
    service.fail_transport_audit = True
    async with AsyncClient(
        transport=ASGITransport(app=_app(actor, service), raise_app_exceptions=False),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            "/api/v1/skills",
            json={"name": "Facilitation", "unexpected": True},
            headers={"Idempotency-Key": "audit-failure-key"},
        )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "AUDIT_UNAVAILABLE"


@pytest.mark.asyncio
async def test_person_skill_put_requires_if_match_only_for_an_update_and_returns_replay() -> None:
    actor = _actor()
    service = StubPeopleCapacityService(actor)
    member_id, skill_id = service.person_skill.membership_id, service.skill.id
    body: dict[str, object] = {
        "skill_id": str(skill_id),
        "level": 4,
        "evidence": list[object](),
    }
    async with AsyncClient(
        transport=ASGITransport(app=_app(actor, service)), base_url="http://testserver"
    ) as client:
        response = await client.put(
            f"/api/v1/members/{member_id}/skills/{skill_id}",
            json=body,
            headers={"Idempotency-Key": "person-skill-key1", "If-Match": '"2"'},
        )

    assert response.status_code == 200
    assert response.headers["etag"] == '"2"'
    assert response.headers["idempotency-replayed"] == "true"
    assert response.json()["evidence"] == []
    call = next(values for name, values in service.calls if name == "set_person_skill")
    assert call["expected_version"] == 2
    assert call["skill_id"] == skill_id


@pytest.mark.asyncio
async def test_person_skill_invalid_evidence_uses_structured_validation_error() -> None:
    actor = _actor()
    service = StubPeopleCapacityService(actor)
    member_id, skill_id = service.person_skill.membership_id, service.skill.id
    async with AsyncClient(
        transport=ASGITransport(app=_app(actor, service)), base_url="http://testserver"
    ) as client:
        response = await client.put(
            f"/api/v1/members/{member_id}/skills/{skill_id}",
            json={
                "skill_id": str(skill_id),
                "level": 4,
                "evidence": [
                    {
                        "evidence_type": "COMPLETED_TASK",
                        "summary": "Wrong source kind",
                        "source_resource_type": "review",
                        "source_resource_id": str(uuid4()),
                        "occurred_at": "2026-08-26T00:00:00Z",
                    }
                ],
            },
            headers={"Idempotency-Key": "invalid-evidence-1"},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"
    assert response.json()["error"]["field_errors"] == [
        {
            "field": "source_resource_type",
            "code": "PEOPLESKILLREFERENCEERROR",
            "message_key": "validation.invalid",
        }
    ]


@pytest.mark.asyncio
async def test_person_skill_domain_normalization_error_is_structured() -> None:
    actor = _actor()
    service = StubPeopleCapacityService(actor)
    member_id, skill_id = service.person_skill.membership_id, service.skill.id
    async with AsyncClient(
        transport=ASGITransport(app=_app(actor, service)), base_url="http://testserver"
    ) as client:
        response = await client.put(
            f"/api/v1/members/{member_id}/skills/{skill_id}",
            json={
                "skill_id": str(skill_id),
                "level": 4,
                "evidence": [
                    {
                        "evidence_type": "MANAGER_NOTE",
                        "summary": "Evidence",
                        "source_resource_type": "   ",
                        "source_resource_id": str(uuid4()),
                        "occurred_at": "2026-08-26T00:00:00Z",
                    }
                ],
            },
            headers={"Idempotency-Key": "domain-validation1"},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


def test_openapi_exposes_complete_people_skills_crud() -> None:
    actor = _actor()
    schema = _app(actor, StubPeopleCapacityService(actor)).openapi()

    assert set(schema["paths"]["/api/v1/skills"]) == {"get", "post"}
    assert set(schema["paths"]["/api/v1/skills/{skill_id}"]) == {"get", "patch", "delete"}
    member_path = "/api/v1/members/{membership_id}/skills/{skill_id}"
    assert set(schema["paths"][member_path]) == {"get", "put", "delete"}
    assert set(schema["paths"]["/api/v1/members/{membership_id}/skills"]) == {"get"}
    assert set(schema["paths"]["/api/v1/members/{membership_id}/work-evidence"]) == {"get", "post"}
    assert "503" in schema["paths"]["/api/v1/skills"]["post"]["responses"]


@pytest.mark.integration
@pytest.mark.skipif(os.getenv("RUN_POSTGRES_INTEGRATION") != "1", reason="requires PostgreSQL")
@pytest.mark.asyncio
async def test_postgres_people_skills_authorization_idempotency_and_stale_contract() -> None:
    organization_id, foreign_organization_id = uuid4(), uuid4()
    manager_user, admin_user, employee_user, foreign_user = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    manager_id, admin_id, employee_id, inactive_id, foreign_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    project_id, completed_task_id = uuid4(), uuid4()
    slug = f"people-api-{organization_id.hex}"
    password = "PeopleIntegration123!"
    manager_email = f"manager-{manager_user.hex}@example.test"
    admin_email = f"admin-{admin_user.hex}@example.test"
    employee_email = f"employee-{employee_user.hex}@example.test"
    settings = Settings(
        environment="test",
        local_auth_organization_slug=slug,
        session_cookie_name=f"people_session_{organization_id.hex}",
    )
    engine = create_database_engine(settings)
    app: FastAPI | None = None
    try:
        encoded = PasswordHash.recommended().hash(password)
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO organizations (id, slug, name) VALUES "
                    "(:id, :slug, 'People Tenant'), "
                    "(:foreign_id, :foreign_slug, 'Foreign People Tenant')"
                ),
                {
                    "id": organization_id,
                    "slug": slug,
                    "foreign_id": foreign_organization_id,
                    "foreign_slug": f"foreign-people-{foreign_organization_id.hex}",
                },
            )
            for user_id, email in (
                (manager_user, manager_email),
                (admin_user, admin_email),
                (employee_user, employee_email),
                (foreign_user, f"foreign-{foreign_user.hex}@example.test"),
            ):
                await connection.execute(
                    text(
                        "INSERT INTO users "
                        "(id, email_normalized, email_display, display_name, password_hash) "
                        "VALUES (:id, :email, :email, 'Person', :password_hash)"
                    ),
                    {"id": user_id, "email": email, "password_hash": encoded},
                )
            await connection.execute(
                text(
                    "INSERT INTO memberships "
                    "(id, organization_id, user_id, role, is_active) VALUES "
                    "(:manager_id, :organization_id, :manager_user, 'MANAGER', true), "
                    "(:admin_id, :organization_id, :admin_user, 'ADMIN', true), "
                    "(:employee_id, :organization_id, :employee_user, 'EMPLOYEE', true), "
                    "(:inactive_id, :organization_id, :foreign_user, 'EMPLOYEE', false), "
                    "(:foreign_id, :foreign_organization_id, :foreign_user, 'EMPLOYEE', true)"
                ),
                {
                    "manager_id": manager_id,
                    "admin_id": admin_id,
                    "employee_id": employee_id,
                    "inactive_id": inactive_id,
                    "foreign_id": foreign_id,
                    "organization_id": organization_id,
                    "foreign_organization_id": foreign_organization_id,
                    "manager_user": manager_user,
                    "admin_user": admin_user,
                    "employee_user": employee_user,
                    "foreign_user": foreign_user,
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO projects "
                    "(id, organization_id, name, created_by_membership_id, "
                    "updated_by_membership_id) "
                    "VALUES (:id, :organization_id, 'Evidence Project', "
                    ":manager_id, :manager_id)"
                ),
                {
                    "id": project_id,
                    "organization_id": organization_id,
                    "manager_id": manager_id,
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO tasks "
                    "(id, organization_id, project_id, title, assignee_membership_id, "
                    "status, version, created_by_membership_id, updated_by_membership_id) "
                    "VALUES (:id, :organization_id, :project_id, 'Completed evidence task', "
                    ":employee_id, 'DONE', 4, :manager_id, :manager_id)"
                ),
                {
                    "id": completed_task_id,
                    "organization_id": organization_id,
                    "project_id": project_id,
                    "employee_id": employee_id,
                    "manager_id": manager_id,
                },
            )

        app = create_app(settings)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            assert (
                await client.post(
                    "/api/v1/auth/login",
                    json={"email": manager_email, "password": password},
                )
            ).status_code == 200
            body = {"name": "Delivery Planning", "description": "Plan delivery"}
            created = await client.post(
                "/api/v1/skills",
                json=body,
                headers={"Idempotency-Key": "people-skill-create-1"},
            )
            replayed = await client.post(
                "/api/v1/skills",
                json=body,
                headers={"Idempotency-Key": "people-skill-create-1"},
            )
            reused = await client.post(
                "/api/v1/skills",
                json={**body, "name": "Different"},
                headers={"Idempotency-Key": "people-skill-create-1"},
            )
            duplicate = await client.post(
                "/api/v1/skills",
                json={"name": "  delivery   planning  ", "description": None},
                headers={"Idempotency-Key": "people-skill-create-2"},
            )
            assert created.status_code == 201
            assert replayed.headers["Idempotency-Replayed"] == "true"
            assert reused.status_code == 409
            assert duplicate.status_code == 409
            skill_id = str(created.json()["id"])

            invalid_transport_body = await client.post(
                "/api/v1/skills",
                json={"name": "Rejected", "unexpected": True},
                headers={"Idempotency-Key": "people-invalid-body1"},
            )
            missing_transport_key = await client.post(
                "/api/v1/skills",
                json={"name": "Rejected without key"},
            )
            invalid_transport_etag = await client.patch(
                f"/api/v1/skills/{skill_id}",
                json={"description": "Rejected ETag"},
                headers={
                    "Idempotency-Key": "people-invalid-etag1",
                    "If-Match": "invalid",
                },
            )
            assert invalid_transport_body.status_code == 422
            assert missing_transport_key.status_code == 422
            assert invalid_transport_etag.status_code == 400

            updated = await client.patch(
                f"/api/v1/skills/{skill_id}",
                json={"description": "Updated"},
                headers={"Idempotency-Key": "people-skill-update-1", "If-Match": '"1"'},
            )
            stale = await client.patch(
                f"/api/v1/skills/{skill_id}",
                json={"description": "Stale"},
                headers={"Idempotency-Key": "people-skill-update-2", "If-Match": '"1"'},
            )
            assert updated.status_code == 200
            assert updated.json()["version"] == 2
            assert stale.status_code == 412

            person_body: dict[str, object] = {
                "skill_id": skill_id,
                "level": 4,
                "evidence": [
                    {
                        "evidence_type": "MANAGER_NOTE",
                        "summary": "Initial evidence",
                        "source_resource_type": "review",
                        "source_resource_id": str(uuid4()),
                        "occurred_at": "2026-08-20T00:00:00Z",
                    }
                ],
            }
            person_skill = await client.put(
                f"/api/v1/members/{employee_id}/skills/{skill_id}",
                json=person_body,
                headers={"Idempotency-Key": "person-skill-create-1"},
            )
            inactive = await client.put(
                f"/api/v1/members/{inactive_id}/skills/{skill_id}",
                json=person_body,
                headers={"Idempotency-Key": "person-skill-inactive"},
            )
            foreign = await client.put(
                f"/api/v1/members/{foreign_id}/skills/{skill_id}",
                json=person_body,
                headers={"Idempotency-Key": "person-skill-foreign1"},
            )
            assert person_skill.status_code == 200
            assert person_skill.json()["evidence"][0]["source_resource_type"] == "manager_note"
            assert person_skill.json()["evidence"][0]["source_resource_id"] == str(manager_id)
            assert inactive.status_code == 422
            assert foreign.status_code == 422
            assert (
                await client.get(f"/api/v1/members/{inactive_id}/skills/{skill_id}")
            ).status_code == 404
            assert (await client.get(f"/api/v1/members/{foreign_id}/skills")).status_code == 404
            updated_person_skill = await client.put(
                f"/api/v1/members/{employee_id}/skills/{skill_id}",
                json={
                    "skill_id": skill_id,
                    "level": 5,
                    "evidence": [
                        {
                            "evidence_type": "MANAGER_NOTE",
                            "summary": "Later evidence",
                            "source_resource_type": "review",
                            "source_resource_id": str(uuid4()),
                            "occurred_at": "2026-08-21T00:00:00Z",
                        }
                    ],
                },
                headers={
                    "Idempotency-Key": "person-skill-update-1",
                    "If-Match": '"1"',
                },
            )
            replayed_person_skill = await client.put(
                f"/api/v1/members/{employee_id}/skills/{skill_id}",
                json=person_body,
                headers={"Idempotency-Key": "person-skill-create-1"},
            )
            reused_person_skill = await client.put(
                f"/api/v1/members/{employee_id}/skills/{skill_id}",
                json={**person_body, "level": 3},
                headers={"Idempotency-Key": "person-skill-create-1"},
            )
            stale_person_skill = await client.put(
                f"/api/v1/members/{employee_id}/skills/{skill_id}",
                json={**person_body, "level": 3},
                headers={
                    "Idempotency-Key": "person-skill-stale-1",
                    "If-Match": '"1"',
                },
            )
            assert updated_person_skill.json()["version"] == 2
            assert replayed_person_skill.json()["version"] == 1
            assert [item["summary"] for item in replayed_person_skill.json()["evidence"]] == [
                "Initial evidence"
            ]
            assert reused_person_skill.status_code == 409
            assert stale_person_skill.status_code == 412

            work_evidence_body = {
                "evidence_type": "COMPLETED_TASK",
                "summary": "Completed assigned work",
                "source_resource_type": "task",
                "source_resource_id": str(completed_task_id),
                "source_resource_version": 4,
                "observed_at": "2026-08-22T00:00:00Z",
            }
            work_evidence = await client.post(
                f"/api/v1/members/{employee_id}/work-evidence",
                json=work_evidence_body,
                headers={"Idempotency-Key": "work-evidence-create-1"},
            )
            replayed_work_evidence = await client.post(
                f"/api/v1/members/{employee_id}/work-evidence",
                json=work_evidence_body,
                headers={"Idempotency-Key": "work-evidence-create-1"},
            )
            duplicate_work_evidence = await client.post(
                f"/api/v1/members/{employee_id}/work-evidence",
                json=work_evidence_body,
                headers={"Idempotency-Key": "work-evidence-create-2"},
            )
            wrong_version = await client.post(
                f"/api/v1/members/{employee_id}/work-evidence",
                json={**work_evidence_body, "source_resource_version": 3},
                headers={"Idempotency-Key": "work-evidence-version1"},
            )
            wrong_member = await client.post(
                f"/api/v1/members/{manager_id}/work-evidence",
                json=work_evidence_body,
                headers={"Idempotency-Key": "work-evidence-member-1"},
            )
            assert work_evidence.status_code == 201
            assert replayed_work_evidence.headers["Idempotency-Replayed"] == "true"
            assert duplicate_work_evidence.status_code == 409
            assert wrong_version.status_code == 422
            assert wrong_member.status_code == 422
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE tasks SET status = 'IN_PROGRESS', version = 5 WHERE id = :task_id"
                    ),
                    {"task_id": completed_task_id},
                )
            replay_after_task_changed = await client.post(
                f"/api/v1/members/{employee_id}/work-evidence",
                json=work_evidence_body,
                headers={"Idempotency-Key": "work-evidence-create-1"},
            )
            assert replay_after_task_changed.status_code == 201
            assert replay_after_task_changed.headers["Idempotency-Replayed"] == "true"

            await client.post("/api/v1/auth/logout")
            assert (
                await client.post(
                    "/api/v1/auth/login",
                    json={"email": admin_email, "password": password},
                )
            ).status_code == 200
            admin_created = await client.post(
                "/api/v1/skills",
                json={"name": "Admin-created skill"},
                headers={"Idempotency-Key": "admin-skill-create-1"},
            )
            assert admin_created.status_code == 201

            await client.post("/api/v1/auth/logout")
            assert (
                await client.post(
                    "/api/v1/auth/login",
                    json={"email": employee_email, "password": password},
                )
            ).status_code == 200
            assert (await client.get("/api/v1/skills")).status_code == 200
            assert (await client.get(f"/api/v1/members/{employee_id}/skills")).status_code == 200
            malformed_forbidden = await client.post("/api/v1/skills", json={"unexpected": True})
            forbidden = await client.post(
                "/api/v1/skills",
                json={"name": "Forbidden"},
                headers={"Idempotency-Key": "employee-skill-write"},
            )
            forbidden_patch = await client.patch(
                f"/api/v1/skills/{skill_id}",
                json={"description": "Forbidden"},
                headers={
                    "Idempotency-Key": "employee-skill-patch",
                    "If-Match": '"2"',
                },
            )
            forbidden_delete = await client.delete(
                f"/api/v1/skills/{skill_id}",
                headers={
                    "Idempotency-Key": "employee-skill-delete",
                    "If-Match": '"2"',
                },
            )
            forbidden_person_put = await client.put(
                f"/api/v1/members/{employee_id}/skills/{skill_id}",
                json=person_body,
                headers={"Idempotency-Key": "employee-person-skill-put"},
            )
            forbidden_person_delete = await client.delete(
                f"/api/v1/members/{employee_id}/skills/{skill_id}",
                headers={
                    "Idempotency-Key": "employee-person-skill-delete",
                    "If-Match": '"2"',
                },
            )
            forbidden_work_evidence = await client.post(
                f"/api/v1/members/{employee_id}/work-evidence",
                json=work_evidence_body,
                headers={"Idempotency-Key": "employee-work-evidence"},
            )
            assert malformed_forbidden.status_code == 403
            assert forbidden.status_code == 403
            assert forbidden_patch.status_code == 403
            assert forbidden_delete.status_code == 403
            assert forbidden_person_put.status_code == 403
            assert forbidden_person_delete.status_code == 403
            assert forbidden_work_evidence.status_code == 403

            await client.post("/api/v1/auth/logout")
            assert (
                await client.post(
                    "/api/v1/auth/login",
                    json={"email": manager_email, "password": password},
                )
            ).status_code == 200
            deleted_person_skill = await client.delete(
                f"/api/v1/members/{employee_id}/skills/{skill_id}",
                headers={
                    "Idempotency-Key": "person-skill-delete-1",
                    "If-Match": '"2"',
                },
            )
            assert deleted_person_skill.status_code == 200
            assert deleted_person_skill.json()["active"] is False
            assert deleted_person_skill.json()["version"] == 3
            tombstone = await client.get(f"/api/v1/members/{employee_id}/skills/{skill_id}")
            assert tombstone.status_code == 200
            assert tombstone.headers["ETag"] == '"3"'
            assert tombstone.json()["active"] is False
            tombstone_list = await client.get(f"/api/v1/members/{employee_id}/skills")
            listed_tombstone = next(
                item for item in tombstone_list.json() if item["skill_id"] == skill_id
            )
            assert listed_tombstone["active"] is False
            reactivated_person_skill = await client.put(
                f"/api/v1/members/{employee_id}/skills/{skill_id}",
                json={**person_body, "level": 5},
                headers={
                    "Idempotency-Key": "person-skill-reactivate-1",
                    "If-Match": '"3"',
                },
            )
            assert reactivated_person_skill.status_code == 200
            assert reactivated_person_skill.json()["active"] is True
            assert reactivated_person_skill.json()["version"] == 4
            deleted_skill = await client.delete(
                f"/api/v1/skills/{skill_id}",
                headers={
                    "Idempotency-Key": "people-skill-delete-1",
                    "If-Match": '"2"',
                },
            )
            assert deleted_skill.status_code == 200
            assert deleted_skill.json()["active"] is False
            assert deleted_skill.json()["version"] == 3
            replay_after_skill_changed = await client.put(
                f"/api/v1/members/{employee_id}/skills/{skill_id}",
                json=person_body,
                headers={"Idempotency-Key": "person-skill-create-1"},
            )
            assert replay_after_skill_changed.status_code == 200
            assert replay_after_skill_changed.json()["version"] == 1
            assert replay_after_skill_changed.headers["Idempotency-Replayed"] == "true"

        await app.state.database_engine.dispose()
        app = None
        async with engine.connect() as connection:
            rejected = await connection.scalar(
                text(
                    "SELECT count(*) FROM audit_events "
                    "WHERE organization_id = :organization_id AND outcome = 'REJECTED'"
                ),
                {"organization_id": organization_id},
            )
            assert rejected == 20
    finally:
        if app is not None:
            await app.state.database_engine.dispose()
        await engine.dispose()
