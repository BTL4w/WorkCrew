"""Synthetic local demo fixtures; semantic interpretation in hosted mode remains model-owned."""

import re
from copy import deepcopy
from typing import Any, cast


def mock_missing_planning_goal(message: str) -> bool:
    """Synthetic intent fixtures only; hosted interpretation remains model-owned."""
    return " ".join(message.casefold().strip(" .?!").split()) in {
        "",
        "tạo plan cho tôi",
        "tạo kế hoạch cho tôi",
        "lập plan",
        "lập kế hoạch",
        "kế hoạch",
        "plan",
        "create a plan for me",
        "make a plan",
        "create a plan",
        "ừ",
        "yeah",
    }


def mock_missing_goal_question(locale: str) -> str:
    return (
        "Thiếu mục tiêu hoặc kết quả cụ thể cần đạt để lập kế hoạch. "
        "Bạn muốn lập kế hoạch cho việc gì?"
        if locale == "vi"
        else "Missing information: the concrete goal or desired outcome. "
        "What would you like the plan to achieve?"
    )


def progressive_fixture(payload: dict[str, object], locale: str) -> dict[str, Any]:
    vi = locale == "vi"
    message = str(payload.get("user_message", "")).casefold()
    refs = cast(list[str], payload.get("permitted_source_refs", []))
    facts = cast(dict[str, object], payload.get("permitted_facts", {}))
    source = str(
        facts.get("current_message_ref")
        or next((ref for ref in refs if ref.startswith("message:")), refs[0] if refs else "")
    )
    current = payload.get("current_draft")
    if current is None and mock_missing_planning_goal(message):
        return {
            "decision": {
                "kind": "ASK_GOAL",
                "draft": None,
                "question": {
                    "id": "missing-goal",
                    "purpose": "GOAL",
                    "text": mock_missing_goal_question(locale),
                    "blocking": True,
                },
            }
        }

    def stated(value: object) -> dict[str, Any]:
        return {"value": value, "status": "USER_STATED", "source_refs": [source]}

    def assumed(value: object, reason: str) -> dict[str, Any]:
        return {
            "value": value,
            "status": "AI_ASSUMPTION",
            "source_refs": [],
            "assumption_reason": reason,
        }

    assessment = None
    if isinstance(current, dict):
        draft = deepcopy(cast(dict[str, Any], current))
        paths: list[str] = []
        tasks: list[str] = []
        money = re.search(r"(\d+)\s*(?:triệu|million)", message)
        if money:
            amount = int(money.group(1)) * 1000000
            draft["brief"]["budget"] = {
                "amount": amount,
                "currency": "VND",
                "status": "USER_STATED",
                "source_refs": [source],
                "assumption_reason": None,
            }
            paths.append("brief.budget")
            draft["tasks"][1]["title"] = (
                (
                    "Thu hút người học qua kênh tiết kiệm"
                    if vi
                    else "Acquire learners through cost-conscious channels"
                )
                if amount <= 10000000
                else ("Marketing và thu hút người học" if vi else "Market and acquire learners")
            )
            tasks.append(draft["tasks"][1]["ref"])
        weeks = re.search(r"(\d+)\s*(?:tuần|weeks?)", message)
        if "six weeks" in message:
            days = 42
        elif weeks:
            days = int(weeks.group(1)) * 7
        elif "two weeks" in message:
            days = 14
        else:
            days = None
        if days:
            draft["duration_days"] = days
            draft["brief"]["timing"] = stated(days)
            paths.extend(["brief.timing", "duration_days"])
        assessment = {
            "affected_field_paths": paths,
            "affected_phase_refs": [],
            "affected_task_refs": tasks,
            "source_refs": [source],
            "reason": "Update only constraints supplied in this message.",
        }
    else:
        recruitment = any(word in message for word in ("tuyển dụng", "recruitment"))
        course = any(word in message for word in ("khóa học", "course"))
        objective = stated(str(payload.get("user_message", "")).strip())
        titles = (
            ["Chuẩn bị khóa học", "Marketing và thu hút", "Mở đăng ký", "Tối ưu và chốt đăng ký"]
            if vi
            else [
                "Prepare course",
                "Marketing and acquisition",
                "Open enrollment",
                "Optimize conversion",
            ]
        )
        if recruitment:
            titles = (
                ["Xác định nhu cầu", "Thu hút ứng viên", "Phỏng vấn", "Hoàn tất tuyển dụng"]
                if vi
                else ["Define roles", "Source candidates", "Interview", "Complete hiring"]
            )
        elif not course:
            titles = (
                [
                    "Xác định mục tiêu và phạm vi",
                    "Chuẩn bị nguồn lực",
                    "Triển khai công việc",
                    "Kiểm tra và tổng kết",
                ]
                if vi
                else [
                    "Define objective and scope",
                    "Prepare resources",
                    "Execute work",
                    "Review and wrap up",
                ]
            )
        starts, ends = [0, 7, 21, 28], [14, 28, 35, 42]
        draft = {
            "schema_version": "planning-draft.v1",
            "brief": {
                "objective": objective,
                "audience": stated("Người đi làm" if vi else "Working adults")
                if any(word in message for word in ("người đi làm", "working adults"))
                else assumed(
                    "Đối tượng rộng, cần xác nhận" if vi else "Broad audience, to confirm",
                    "No detailed audience supplied",
                ),
                "timing": assumed(42, "No launch date supplied; six weeks is a proposed duration"),
                "expected_outcomes": [stated("100 học viên" if vi else "100 learners")]
                if course and "100" in message
                else [],
                "scope_in": [],
                "scope_out": [],
                "constraints": [],
                "budget": None,
            },
            "duration_days": 42,
            "phases": [
                {"ref": f"p{i + 1}", "title": title, "start_day": starts[i], "end_day": ends[i]}
                for i, title in enumerate(titles)
            ],
            "tasks": [
                {
                    "ref": f"t{i + 1}",
                    "phase_ref": f"p{i + 1}",
                    "title": title,
                    "description": None,
                    "start_day": starts[i],
                    "end_day": starts[i] + 7,
                    "duration_days": 7,
                    "duration_source": "AI_ASSUMPTION",
                    "duration_source_refs": [],
                    "estimated_effort_hours": 8,
                    "required_skill_labels": [],
                    "acceptance_criteria": ["Kết quả được kiểm tra" if vi else "Output reviewed"],
                }
                for i, title in enumerate(titles)
            ],
            "dependencies": [
                {"predecessor_ref": f"t{i}", "successor_ref": f"t{i + 1}"} for i in range(1, 4)
            ],
            "assumptions": [
                assumed(
                    "Thời gian triển khai dự kiến 6 tuần" if vi else "Proposed six-week launch",
                    "No timeline supplied",
                )
            ],
            "open_questions": [],
        }
    draft["open_questions"] = []
    return {
        "decision": {"kind": "DRAFT", "question": None, "draft": draft},
        "change_assessment": assessment,
    }
