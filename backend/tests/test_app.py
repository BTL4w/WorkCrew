"""Smoke and transport-contract tests for the FastAPI foundation."""

from collections.abc import Mapping
from typing import Annotated

import pytest
from fastapi import FastAPI, Path
from httpx import ASGITransport, AsyncClient, Response

from app.api.errors import ApplicationError
from app.core.config import Settings
from app.main import create_app


def _test_app() -> FastAPI:
    return create_app(Settings(environment="test"))


async def _get(
    app: FastAPI,
    path: str,
    *,
    headers: Mapping[str, str] | None = None,
    raise_app_exceptions: bool = True,
) -> Response:
    transport = ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.get(path, headers=headers)


@pytest.mark.asyncio
async def test_openapi_document_is_available() -> None:
    response = await _get(_test_app(), "/openapi.json")

    assert response.status_code == 200
    assert response.json()["info"]["title"] == "Work Management API"
    assert set(response.json()["paths"]) == {
        "/api/v1/risks",
        "/api/v1/risks/refresh",
        "/api/v1/risks/weeks/{week_id}",
        "/api/v1/risks/{risk_id}",
        "/api/v1/risks/{risk_id}/reviews",
        "/api/v1/notifications",
        "/api/v1/notifications/{notification_id}/read",
        "/api/v1/blockers",
        "/api/v1/blockers/{blocker_id}",
        "/api/v1/blockers/{blocker_id}/history",
        "/api/v1/daily-updates",
        "/api/v1/daily-updates/drafts",
        "/api/v1/daily-updates/drafts/{draft_id}",
        "/api/v1/daily-updates/{draft_id}/draft",
        "/api/v1/daily-updates/{draft_id}/confirm",
        "/api/v1/daily-updates/{draft_id}/assess",
        "/api/v1/daily-updates/{draft_id}/evidence-assessments",
        "/api/v1/tasks/{task_id}/reporting-context",
        "/api/v1/evidence",
        "/api/v1/evidence/{evidence_id}/versions/{version}",
        "/api/v1/evidence/{evidence_id}/versions/{version}/content",
        "/api/v1/auth/login",
        "/api/v1/auth/logout",
        "/api/v1/me",
        "/api/v1/projects",
        "/api/v1/projects/{project_id}",
        "/api/v1/projects/{project_id}/weeks",
        "/api/v1/projects/{project_id}/weeks/{week_id}/progress",
        "/api/v1/projects/{project_id}/team-requirements",
        "/api/v1/projects/{project_id}/team-requirements/ranking-preview",
        "/api/v1/projects/{project_id}/team",
        "/api/v1/projects/{project_id}/team-recommendations",
        "/api/v1/recommendations/{recommendation_id}",
        "/api/v1/recommendations/{recommendation_id}/versions/{version}",
        "/api/v1/recommendations/{recommendation_id}/approve",
        "/api/v1/recommendations/{recommendation_id}/feedback",
        "/api/v1/projects/{project_id}/weeks/{project_week_id}",
        "/api/v1/members",
        "/api/v1/skills",
        "/api/v1/skills/{skill_id}",
        "/api/v1/members/{membership_id}/skills",
        "/api/v1/members/{membership_id}/skills/{skill_id}",
        "/api/v1/members/{membership_id}/work-evidence",
        "/api/v1/capacity",
        "/api/v1/capacity/{capacity_id}",
        "/api/v1/leave",
        "/api/v1/leave/{leave_id}",
        "/api/v1/workload",
        "/api/v1/tasks",
        "/api/v1/tasks/{task_id}",
        "/api/v1/tasks/{task_id}/assign",
        "/api/v1/tasks/{task_id}/status",
        "/api/v1/my-tasks",
        "/api/v1/goals",
        "/api/v1/goals/{goal_id}",
        "/api/v1/milestones",
        "/api/v1/milestones/{milestone_id}",
        "/api/v1/task-dependencies",
        "/api/v1/task-dependencies/{dependency_id}",
        "/api/v1/acceptance-criteria",
        "/api/v1/acceptance-criteria/{criterion_id}",
        "/api/v1/ai/planning-runs",
        "/api/v1/workflow-runs",
        "/api/v1/workflow-runs/{run_id}",
        "/api/v1/workflow-runs/{run_id}/messages",
        "/api/v1/workflow-runs/{run_id}/events",
        "/api/v1/proposals/{proposal_id}",
        "/api/v1/proposals/{proposal_id}/versions/{version_number}",
        "/api/v1/approvals/{approval_id}/decision",
        "/api/v1/ai/conversations",
        "/api/v1/ai/conversations/{conversation_id}",
        "/api/v1/ai/conversations/{conversation_id}/messages",
        "/api/v1/ai/conversations/{conversation_id}/events",
    }
    login_responses = response.json()["paths"]["/api/v1/auth/login"]["post"]["responses"]
    assert login_responses["422"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ErrorResponse"
    }
    project_collection = response.json()["paths"]["/api/v1/projects"]
    project_resource = response.json()["paths"]["/api/v1/projects/{project_id}"]
    assert set(project_collection) == {"get", "post"}
    assert set(project_resource) == {"get", "patch"}
    create_parameters = {
        parameter["name"]: parameter for parameter in project_collection["post"]["parameters"]
    }
    update_parameters = {
        parameter["name"]: parameter for parameter in project_resource["patch"]["parameters"]
    }
    assert create_parameters["Idempotency-Key"]["required"] is True
    assert update_parameters["Idempotency-Key"]["required"] is True
    assert "If-Match" in update_parameters
    assert project_collection["post"]["responses"]["201"]["content"]["application/json"][
        "schema"
    ] == {"$ref": "#/components/schemas/ProjectResponse"}
    people_mutations = (
        response.json()["paths"]["/api/v1/skills"]["post"],
        response.json()["paths"]["/api/v1/skills/{skill_id}"]["patch"],
        response.json()["paths"]["/api/v1/skills/{skill_id}"]["delete"],
        response.json()["paths"]["/api/v1/members/{membership_id}/skills/{skill_id}"]["put"],
        response.json()["paths"]["/api/v1/members/{membership_id}/skills/{skill_id}"]["delete"],
        response.json()["paths"]["/api/v1/members/{membership_id}/work-evidence"]["post"],
    )
    for operation in people_mutations:
        parameters = {item["name"]: item for item in operation["parameters"]}
        assert parameters["Idempotency-Key"]["required"] is True
    for operation in (*people_mutations[1:3], people_mutations[4]):
        parameters = {item["name"]: item for item in operation["parameters"]}
        assert parameters["If-Match"]["required"] is True
    assert set(response.json()["paths"]["/api/v1/tasks"]) == {"get", "post"}
    assert set(response.json()["paths"]["/api/v1/tasks/{task_id}"]) == {"get", "patch"}
    assert set(response.json()["paths"]["/api/v1/tasks/{task_id}/status"]) == {"post"}
    assert response.headers["X-Request-ID"]


@pytest.mark.asyncio
async def test_unknown_api_route_uses_structured_error_contract() -> None:
    response = await _get(
        _test_app(),
        "/api/v1/unknown",
        headers={"X-Request-ID": "learning-run-1"},
    )

    assert response.status_code == 404
    assert response.headers["X-Request-ID"] == "learning-run-1"
    assert response.json() == {
        "error": {
            "code": "RESOURCE_NOT_FOUND",
            "message_key": "common.error.notFound",
            "request_id": "learning-run-1",
            "field_errors": [],
            "details": {},
        }
    }


@pytest.mark.asyncio
async def test_request_validation_uses_structured_error_contract() -> None:
    app = _test_app()

    @app.get("/api/v1/_test/items/{item_id}")
    async def get_test_item(  # pyright: ignore[reportUnusedFunction]
        item_id: Annotated[int, Path(gt=0)],
    ) -> dict[str, int]:
        return {"item_id": item_id}

    response = await _get(app, "/api/v1/_test/items/not-an-integer")

    assert response.status_code == 422
    payload = response.json()["error"]
    assert payload["code"] == "VALIDATION_FAILED"
    assert payload["message_key"] == "common.error.validation"
    assert payload["field_errors"][0]["field"] == "path.item_id"
    assert "input" not in payload


@pytest.mark.asyncio
async def test_invalid_json_reports_body_in_structured_error_contract() -> None:
    app = _test_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/api/v1/auth/login",
            content='{"email":"manager@example.test","password":"missing-quote}',
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"
    assert response.json()["error"]["field_errors"] == [
        {
            "field": "body",
            "code": "JSON_INVALID",
            "message_key": "validation.invalid",
        }
    ]


@pytest.mark.asyncio
async def test_unexpected_error_does_not_expose_internal_details() -> None:
    app = _test_app()

    @app.get("/api/v1/_test/failure")
    async def fail_for_test() -> None:  # pyright: ignore[reportUnusedFunction]
        raise RuntimeError("private failure detail")

    response = await _get(app, "/api/v1/_test/failure", raise_app_exceptions=False)

    assert response.status_code == 500
    assert response.headers["X-Request-ID"]
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert "private failure detail" not in response.text


@pytest.mark.asyncio
async def test_expected_application_error_keeps_safe_structured_details() -> None:
    app = _test_app()

    @app.get("/api/v1/_test/conflict")
    async def conflict_for_test() -> None:  # pyright: ignore[reportUnusedFunction]
        raise ApplicationError(
            status_code=409,
            code="RESOURCE_VERSION_MISMATCH",
            message_key="common.error.resourceVersionMismatch",
            details={"current_version": 2},
        )

    response = await _get(app, "/api/v1/_test/conflict")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RESOURCE_VERSION_MISMATCH"
    assert response.json()["error"]["details"] == {"current_version": 2}
