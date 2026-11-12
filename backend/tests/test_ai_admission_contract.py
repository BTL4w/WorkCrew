"""Admission extends the existing envelope and only declares new-work errors."""

import json
from pathlib import Path

from app.main import app


def test_admission_openapi_matches_frontend_and_retry_headers() -> None:
    schema = app.openapi()
    manifest = json.loads(
        (
            Path(__file__).resolve().parents[2] / "frontend/src/features/work/openapi-contract.json"
        ).read_text()
    )["aiAdmission"]
    for path in manifest["paths"]:
        responses = schema["paths"][path]["post"]["responses"]
        for status in ("429", "503"):
            assert responses[status]["content"]["application/json"]["schema"] == {
                "$ref": "#/components/schemas/ErrorResponse"
            }
            assert responses[status]["headers"]["Retry-After"]["schema"]["type"] == "integer"
