"""Trace the public Assistant API for a Project-only planning turn.

Read-only by default. ``--send`` creates a conversation and a planning request,
but never approves a proposal or creates a business project.
"""

from __future__ import annotations

import argparse
import getpass
import http.cookiejar
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

DEFAULT_PROMPT = (
    "Hãy lập kế hoạch cho một project nội bộ tên 'Kiểm thử Project only' "
    "để chuẩn hóa quy trình tiếp nhận yêu cầu trong 2 tuần. "
    "Chỉ tạo Project và plan; không đề xuất Team, không phân công nhân sự."
)
TERMINAL_KINDS = {
    "proposal": "PROPOSAL_READY",
    "question": "WAITING_FOR_USER_INPUT",
    "safe_error": "SAFE_ERROR",
    "capability_unavailable": "CAPABILITY_UNAVAILABLE",
    "team_recommendation": "UNEXPECTED_TEAM_BRANCH",
}


@dataclass(frozen=True)
class SnapshotInspection:
    last_sequence: int
    lines: tuple[str, ...]
    outcome: str | None


def latest_user_sequence(snapshot: dict[str, Any]) -> int:
    return max(
        (
            int(message.get("sequence", 0))
            for message in snapshot.get("messages", [])
            if message.get("role", "").upper() == "USER"
        ),
        default=0,
    )


def describe_block(block: dict[str, Any]) -> str:
    """Print only structured, non-free-text fields from a public content block."""
    kind = str(block.get("kind", "unknown"))
    allowed = {
        "activity": ("status", "label_key", "agent_id", "workflow_run_id"),
        "planning_run": ("status", "workflow_run_id"),
        "proposal": ("state", "workflow_run_id", "proposal_id", "proposal_version", "can_approve"),
        "question": (),
        "safe_error": ("code", "message_key"),
        "capability_unavailable": ("capability", "message_key"),
        "team_recommendation": ("status", "recommendation_id"),
        "decision_result": ("decision", "project_id"),
    }.get(kind, ())
    details = " ".join(f"{key}={block[key]}" for key in allowed if block.get(key) is not None)
    return f"{kind} {details}".rstrip()


def inspect_snapshot(snapshot: dict[str, Any], *, after_sequence: int) -> SnapshotInspection:
    lines: list[str] = []
    outcome: str | None = None
    last_sequence = after_sequence
    for message in sorted(snapshot.get("messages", []), key=lambda item: item.get("sequence", 0)):
        sequence = int(message.get("sequence", 0))
        if sequence <= after_sequence:
            continue
        last_sequence = max(last_sequence, sequence)
        if message.get("role", "").upper() != "ASSISTANT":
            continue
        for block in message.get("content_blocks", []):
            kind = block.get("kind")
            lines.append(f"message #{sequence}: {describe_block(block)}")
            candidate = TERMINAL_KINDS.get(kind)
            if candidate:
                outcome = candidate
    return SnapshotInspection(last_sequence, tuple(lines), outcome)


class ApiError(Exception):
    pass


class AssistantApi:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/") + "/api/v1"
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )

    def request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
            if method == "POST" and path.startswith("/ai/conversations"):
                headers["Idempotency-Key"] = str(uuid4())
        request = urllib.request.Request(
            self.base_url + path, data=data, headers=headers, method=method
        )
        try:
            with self.opener.open(request, timeout=10) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            try:
                payload = json.load(error)
                code = payload.get("error", {}).get("code") or payload.get("code")
            except (ValueError, AttributeError):
                code = None
            raise ApiError(f"HTTP {error.code} {code or error.reason}") from error
        except (urllib.error.URLError, TimeoutError) as error:
            raise ApiError(f"API unavailable: {type(error).__name__}") from error

    def login(self, email: str, password: str) -> None:
        self.request("POST", "/auth/login", {"email": email, "password": password})

    def conversation(self, conversation_id: str) -> dict[str, Any]:
        return self.request("GET", f"/ai/conversations/{conversation_id}")


def validate_local_url(base_url: str) -> None:
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1"}:
        raise ValueError("Only local HTTP URLs are supported, to protect login credentials")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("Use the API origin only, e.g. http://127.0.0.1:8000")


def run(args: argparse.Namespace) -> int:
    validate_local_url(args.api_base)
    password = os.environ.get("ASSISTANT_TEST_PASSWORD") or getpass.getpass("Manager password: ")
    api = AssistantApi(args.api_base)
    api.login(args.email, password)
    del password

    if args.send:
        created = api.request(
            "POST", "/ai/conversations", {"locale": "vi", "title": "Chẩn đoán Project only"}
        )
        conversation_id = created["id"]
        accepted = api.request(
            "POST",
            f"/ai/conversations/{conversation_id}/messages",
            {"message": args.prompt, "locale": "vi"},
        )
        print(
            f"accepted: status={accepted['status']} conversation_id={conversation_id} "
            f"turn_id={accepted['turn_id']} orchestration_run_id={accepted['orchestration_run_id']}"
        )
        last_sequence = 0
    else:
        if args.conversation_id:
            conversation_id = str(UUID(args.conversation_id))
        else:
            items = api.request("GET", "/ai/conversations?limit=1").get("items", [])
            if not items:
                raise ApiError("No conversations found. Use --send to create a test turn.")
            conversation_id = items[0]["id"]
        print(f"watching: conversation_id={conversation_id} (read-only)")
        last_sequence = latest_user_sequence(api.conversation(conversation_id))
        print(f"watching latest user turn: sequence={last_sequence}")

    started = time.monotonic()
    next_heartbeat = started + 10
    last_detail = "no assistant message yet"
    while True:
        snapshot = api.conversation(conversation_id)
        inspection = inspect_snapshot(snapshot, after_sequence=last_sequence)
        for line in inspection.lines:
            print(line)
            last_detail = line
        last_sequence = inspection.last_sequence
        if inspection.outcome:
            print(f"result: {inspection.outcome}")
            return 0 if inspection.outcome in {"PROPOSAL_READY", "WAITING_FOR_USER_INPUT"} else 1
        now = time.monotonic()
        if now - started >= args.timeout:
            print(f"TIMEOUT after {args.timeout}s; last observed: {last_detail}")
            print("Check worker logs: docker compose logs --since=10m ai-worker")
            return 2
        if now >= next_heartbeat:
            conversation = snapshot.get("conversation", {})
            print(
                f"waiting: conversation_status={conversation.get('status', '?')} "
                f"last_message_sequence={conversation.get('last_message_sequence', '?')} "
                f"last_event_sequence={conversation.get('last_event_sequence', '?')}"
            )
            next_heartbeat = now + 10
        time.sleep(min(args.interval, max(0, args.timeout - (now - started))))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-base", default="http://127.0.0.1:8000")
    parser.add_argument("--email", default="manager@example.test")
    parser.add_argument(
        "--conversation-id", help="Watch an existing conversation (default: newest)"
    )
    parser.add_argument(
        "--send", action="store_true", help="Create and send a Project-only test turn"
    )
    parser.add_argument("--prompt", default=DEFAULT_PROMPT, help="Prompt used only with --send")
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--interval", type=float, default=2.0)
    args = parser.parse_args()
    if args.timeout <= 0 or args.interval <= 0:
        parser.error("--timeout and --interval must be positive")
    if args.send and args.conversation_id:
        parser.error("Use either --send or --conversation-id")
    try:
        return run(args)
    except (ApiError, ValueError) as error:
        print(f"diagnostic error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
