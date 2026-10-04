"""Check the frontend runtime contract against FastAPI's public OpenAPI schema."""

import json
from pathlib import Path
from typing import cast

from app.main import app


def _describe(schema: dict[str, object], schemas: dict[str, object]) -> str:
    reference = schema.get("$ref")
    if isinstance(reference, str):
        name = reference.rsplit("/", 1)[-1]
        target = schemas[name]
        assert isinstance(target, dict)
        return f"enum:{name}" if "enum" in target else f"ref:{name}"

    any_of = schema.get("anyOf")
    if isinstance(any_of, list):
        fragments = cast(list[dict[str, object]], any_of)
        return "|".join(sorted(_describe(item, schemas) for item in fragments))

    enum = schema.get("enum")
    if isinstance(enum, list):
        values = cast(list[object], enum)
        return "literal:" + "|".join(str(value) for value in values)
    if "const" in schema:
        return f"literal:{schema['const']}"

    schema_type = schema.get("type")
    if schema_type == "array":
        if schema.get("maxItems") == 0:
            return "array:never"
        return f"array:{_describe(schema['items'], schemas)}"  # type: ignore[arg-type]
    if schema_type == "string":
        result = "string"
        if schema.get("format"):
            result += f":{schema['format']}"
        if "minLength" in schema or "maxLength" in schema:
            result += f"[{schema.get('minLength', '')},{schema.get('maxLength', '')}]"
        return result
    if schema_type in {"object", "integer", "number", "boolean", "null"}:
        return str(schema_type)
    raise AssertionError(f"Unsupported OpenAPI schema fragment: {schema}")


def test_frontend_work_contract_manifest_matches_openapi() -> None:
    manifest_path = (
        Path(__file__).resolve().parents[2]
        / "frontend"
        / "src"
        / "features"
        / "work"
        / "openapi-contract.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    schemas = cast(
        dict[str, object],
        app.openapi()["components"]["schemas"],
    )

    assert {
        "GoalCreateRequest",
        "GoalUpdateRequest",
        "GoalResponse",
        "MilestoneCreateRequest",
        "MilestoneUpdateRequest",
        "MilestoneResponse",
        "DependencyCreateRequest",
        "DependencyUpdateRequest",
        "DependencyResponse",
        "AcceptanceCriterionCreateRequest",
        "AcceptanceCriterionUpdateRequest",
        "AcceptanceCriterionResponse",
        "PlanningPageResponse",
        "DeleteResponse",
    }.issubset(manifest["schemas"])

    for enum_name, expected in manifest["enums"].items():
        schema = schemas[enum_name]
        assert isinstance(schema, dict)
        assert schema["enum"] == expected

    for schema_name, expected in manifest["schemas"].items():
        schema = cast(dict[str, object], schemas[schema_name])
        properties = cast(dict[str, dict[str, object]], schema["properties"])
        actual = {
            "required": cast(list[str], schema.get("required", [])),
            "properties": {name: _describe(value, schemas) for name, value in properties.items()},
        }
        assert actual == expected, schema_name


def test_assistant_openapi_exposes_typed_transcript_contract() -> None:
    schema = app.openapi()
    manifest_path = (
        Path(__file__).resolve().parents[2]
        / "frontend"
        / "src"
        / "features"
        / "work"
        / "openapi-contract.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))["assistant"]
    schemas = cast(dict[str, object], schema["components"]["schemas"])
    message = schema["components"]["schemas"]["MessageResponse"]
    block_items = message["properties"]["content_blocks"]["items"]

    assert block_items["discriminator"]["propertyName"] == "kind"
    assert block_items["discriminator"]["mapping"] == manifest["content_block_mapping"]

    for path, expected_methods in manifest["paths"].items():
        assert set(schema["paths"][path]) == set(expected_methods)

    for schema_name, expected in manifest["schemas"].items():
        contract = cast(dict[str, object], schemas[schema_name])
        properties = cast(dict[str, dict[str, object]], contract["properties"])
        actual = {
            "required": cast(list[str], contract.get("required", [])),
            "properties": {name: _describe(value, schemas) for name, value in properties.items()},
        }
        assert actual == expected, schema_name

    message_post = schema["paths"]["/api/v1/ai/conversations/{conversation_id}/messages"]["post"]
    assert message_post["responses"]["202"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/AssistantTurnAcceptedResponse"
    }


def test_people_capacity_openapi_paths_match_frontend_manifest() -> None:
    schema = app.openapi()
    manifest_path = (
        Path(__file__).resolve().parents[2]
        / "frontend"
        / "src"
        / "features"
        / "work"
        / "openapi-contract.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))["people_capacity"]

    for path, expected_methods in manifest["paths"].items():
        assert set(schema["paths"][path]) == set(expected_methods)


def test_team_requirements_openapi_matches_frontend_manifest() -> None:
    schema = app.openapi()
    manifest_path = (
        Path(__file__).resolve().parents[2]
        / "frontend"
        / "src"
        / "features"
        / "work"
        / "openapi-contract.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))["team_requirements"]
    schemas = cast(dict[str, object], schema["components"]["schemas"])
    for path, expected_methods in manifest["paths"].items():
        assert set(schema["paths"][path]) == set(expected_methods)
    for schema_name, expected in manifest["schemas"].items():
        contract = cast(dict[str, object], schemas[schema_name])
        properties = cast(dict[str, dict[str, object]], contract["properties"])
        actual = {
            "required": cast(list[str], contract.get("required", [])),
            "properties": {name: _describe(value, schemas) for name, value in properties.items()},
        }
        assert actual == expected, schema_name


def test_team_recommendations_openapi_matches_frontend_manifest() -> None:
    schema = app.openapi()
    manifest_path = (
        Path(__file__).resolve().parents[2]
        / "frontend"
        / "src"
        / "features"
        / "work"
        / "openapi-contract.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))["team_recommendations"]
    schemas = cast(dict[str, object], schema["components"]["schemas"])
    for path, expected_methods in manifest["paths"].items():
        assert set(schema["paths"][path]) == set(expected_methods)
    for schema_name, expected in manifest["schemas"].items():
        contract = cast(dict[str, object], schemas[schema_name])
        properties = cast(dict[str, dict[str, object]], contract["properties"])
        actual = {
            "required": cast(list[str], contract.get("required", [])),
            "properties": {name: _describe(value, schemas) for name, value in properties.items()},
        }
        assert actual == expected, schema_name


def test_explicit_assignment_openapi_matches_frontend_manifest() -> None:
    schema = app.openapi()
    manifest_path = (
        Path(__file__).resolve().parents[2]
        / "frontend"
        / "src"
        / "features"
        / "work"
        / "openapi-contract.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))["explicit_assignment"]
    schemas = cast(dict[str, object], schema["components"]["schemas"])
    for path, expected_methods in manifest["paths"].items():
        assert set(schema["paths"][path]) == set(expected_methods)
    for schema_name, expected in manifest["schemas"].items():
        contract = cast(dict[str, object], schemas[schema_name])
        properties = cast(dict[str, dict[str, object]], contract["properties"])
        actual = {
            "required": cast(list[str], contract.get("required", [])),
            "properties": {name: _describe(value, schemas) for name, value in properties.items()},
        }
        assert actual == expected, schema_name


def test_weekly_progress_openapi_matches_frontend_manifest() -> None:
    schema = app.openapi()
    manifest_path = (
        Path(__file__).resolve().parents[2] / "frontend/src/features/work/openapi-contract.json"
    )
    manifest = json.loads(manifest_path.read_text())["weekly_progress"]
    schemas = cast(dict[str, object], schema["components"]["schemas"])
    for path, methods in manifest["paths"].items():
        assert set(schema["paths"][path]) == set(methods)
    for name, expected in manifest["schemas"].items():
        contract = cast(dict[str, object], schemas[name])
        properties = cast(dict[str, dict[str, object]], contract["properties"])
        assert {
            "required": contract.get("required", []),
            "properties": {key: _describe(value, schemas) for key, value in properties.items()},
        } == expected


def test_daily_updates_openapi_matches_frontend_manifest() -> None:
    schema = app.openapi()
    manifest_path = (
        Path(__file__).resolve().parents[2] / "frontend/src/features/work/openapi-contract.json"
    )
    manifest = json.loads(manifest_path.read_text())["daily_updates"]
    schemas = cast(dict[str, object], schema["components"]["schemas"])
    for path, methods in manifest["paths"].items():
        assert set(schema["paths"][path]) == set(methods)
    for name, expected in manifest["schemas"].items():
        contract = cast(dict[str, object], schemas[name])
        properties = cast(dict[str, dict[str, object]], contract["properties"])
        assert {
            "required": contract.get("required", []),
            "properties": {key: _describe(value, schemas) for key, value in properties.items()},
        } == expected


def test_blocker_openapi_matches_frontend_manifest() -> None:
    schema = app.openapi()
    manifest = json.loads(
        (
            Path(__file__).resolve().parents[2] / "frontend/src/features/work/openapi-contract.json"
        ).read_text()
    )["blockers"]
    schemas = cast(dict[str, object], schema["components"]["schemas"])
    for path, methods in manifest["paths"].items():
        assert set(schema["paths"][path]) == set(methods)
    for name, expected in manifest["schemas"].items():
        contract = cast(dict[str, object], schemas[name])
        properties = cast(dict[str, dict[str, object]], contract["properties"])
        assert {
            "required": contract.get("required", []),
            "properties": {key: _describe(value, schemas) for key, value in properties.items()},
        } == expected


def test_risk_openapi_matches_frontend_manifest() -> None:
    schema = app.openapi()
    path = Path(__file__).resolve().parents[2] / "frontend/src/features/work/openapi-contract.json"
    manifest = json.loads(path.read_text())["risk"]
    schemas = cast(dict[str, object], schema["components"]["schemas"])
    for route, methods in manifest["paths"].items():
        assert set(schema["paths"][route]) == set(methods)
        for method in methods:
            if method == "post":
                assert any(
                    p["name"] == "Idempotency-Key"
                    for p in schema["paths"][route][method]["parameters"]
                )
    for name, expected in manifest["schemas"].items():
        contract = cast(dict[str, object], schemas[name])
        properties = cast(dict[str, dict[str, object]], contract["properties"])
        assert {
            "required": contract.get("required", []),
            "properties": {key: _describe(value, schemas) for key, value in properties.items()},
        } == expected, name


def test_reporting_openapi_matches_frontend_manifest() -> None:
    schema = app.openapi()
    path = Path(__file__).resolve().parents[2] / "frontend/src/features/work/openapi-contract.json"
    manifest = json.loads(path.read_text())["reporting"]
    schemas = cast(dict[str, object], schema["components"]["schemas"])
    assert set(manifest["paths"]) == {
        "/api/v1/reports",
        "/api/v1/reports/defaults",
        "/api/v1/reports/{report_id}",
        "/api/v1/reports/{report_id}/sources",
    }
    for route, methods in manifest["paths"].items():
        assert set(schema["paths"][route]) == set(methods)
    post = schema["paths"]["/api/v1/reports"]["post"]
    assert any(p["name"] == "Idempotency-Key" and p["required"] for p in post["parameters"])
    assert post["responses"]["201"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ReportResponse"
    }
    for name, expected in manifest["schemas"].items():
        contract = cast(dict[str, object], schemas[name])
        properties = cast(dict[str, dict[str, object]], contract["properties"])
        assert {
            "required": contract.get("required", []),
            "properties": {key: _describe(value, schemas) for key, value in properties.items()},
        } == expected, name
