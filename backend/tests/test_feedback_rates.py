from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID, uuid4

from app.modules.feedback.domain.feedback import Feedback
from app.modules.feedback.domain.outcomes import review_rates


def feedback(
    generation: UUID | None = None,
    decision: Literal["ACCEPT", "EDIT", "REJECT"] = "ACCEPT",
    kind: Literal["TERMINAL_QUALITY", "ADVISORY"] = "TERMINAL_QUALITY",
):
    return Feedback(
        id=uuid4(),
        report_id=uuid4(),
        report_version_id=uuid4(),
        original_version_id=uuid4(),
        generation_id=generation or uuid4(),
        actor_membership_id=uuid4(),
        decision_id=uuid4() if kind == "TERMINAL_QUALITY" else None,
        kind=kind,
        decision=decision,
        reason=None,
        provenance={},
        created_at=datetime.now(UTC),
    )


def test_quality_rates_ignore_replays_and_advisory_rows():
    rows = [feedback(), feedback(), feedback(decision="EDIT"), feedback(decision="REJECT")]
    rates = review_rates([*rows, rows[0], feedback(kind="ADVISORY")], pending=2, failed=3, manual=4)
    assert rates.reviewed_generation_count == 4
    assert rates.accept_percent == Decimal("50")
    assert rates.edit_percent == rates.reject_percent == Decimal("25")
    assert (
        rates.pending_generation_count,
        rates.failed_generation_count,
        rates.manual_report_count,
    ) == (2, 3, 4)


def test_edited_publish_is_not_unedited_acceptance():
    rates = review_rates([feedback(decision="EDIT")])
    assert rates.edit_percent == 100
    assert rates.accept_percent == 0


def test_unreviewed_rates_are_unknown():
    rates = review_rates([feedback(kind="ADVISORY")], pending=1)
    assert rates.reviewed_generation_count == 0
    assert rates.accept_percent is rates.edit_percent is rates.reject_percent is None
