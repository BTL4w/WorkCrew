"""Verify citations and structured factual claims against authorized sources."""

import re
from decimal import Decimal, InvalidOperation

from work_management_ai.agents.risk.contracts import RiskExplanation, RiskExplanationInput

_NUMBER = re.compile(r"(?<![\w])\d+(?:[.,]\d+)?(?![\w])")
_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_STATUS = re.compile(r"\b(?:TO_DO|IN_PROGRESS|DONE)\b")


def _number(value: object) -> Decimal | None:
    if isinstance(value, bool):
        return None
    try:
        return Decimal(str(value).replace(",", "."))
    except InvalidOperation:
        return None


def verify_explanation(context: RiskExplanationInput, result: RiskExplanation) -> None:
    sources = {s.id: s.values for s in context.permitted_sources}
    observations = {o.id: o for o in context.observations}
    if len(sources) != len(context.permitted_sources):
        raise ValueError("DUPLICATE_SOURCES")
    assessment_id = f"assessment:{context.risk_assessment_id}"
    assessment = {"score": context.score, "band": context.band, "state": context.state}
    for explanation in result.observation_explanations:
        if not set(explanation.observation_ids).issubset(observations):
            raise ValueError("UNSUPPORTED_OBSERVATION")
        permitted = {s for o in explanation.observation_ids for s in observations[o].source_ids}
        if not set(explanation.source_ids).issubset(sources.keys() & permitted):
            raise ValueError("UNSUPPORTED_SOURCE")
        if not explanation.assertions:
            raise ValueError("FACT_ASSERTIONS_REQUIRED")
        for claim in explanation.assertions:
            if claim.source_id == assessment_id and context.risk_assessment_id is not None:
                facts = assessment
            elif claim.source_id in explanation.source_ids:
                facts = sources[claim.source_id]
            else:
                raise ValueError("UNSUPPORTED_ASSERTION_SOURCE")
            if (
                claim.field not in facts
                or type(facts[claim.field]) is not type(claim.value)
                or facts[claim.field] != claim.value
            ):
                raise ValueError("CONTRADICTORY_ASSERTION")
        values = [a.value for a in explanation.assertions]
        text = explanation.text
        dates = _DATE.findall(text)
        if any(date not in values for date in dates):
            raise ValueError("UNSUPPORTED_DATE")
        # Dates are verified as whole values rather than unrelated component numbers.
        numbers = _NUMBER.findall(_DATE.sub("", text))
        verified_numbers = {_number(value) for value in values}
        if any(_number(value) not in verified_numbers for value in numbers):
            raise ValueError("UNSUPPORTED_NUMBER")
        statuses = set(_STATUS.findall(text))
        localized = text.casefold()
        for phrase, status in (
            ("đã hoàn thành", "DONE"),
            ("đang thực hiện", "IN_PROGRESS"),
            ("chưa bắt đầu", "TO_DO"),
        ):
            if phrase in localized:
                statuses.add(status)
        if any(status not in values for status in statuses):
            raise ValueError("UNSUPPORTED_STATUS")
        if numbers and re.search(r"\b(?:score|điểm)\b", localized):
            score_claims = [
                a
                for a in explanation.assertions
                if a.source_id == assessment_id and a.field == "score"
            ]
            if not score_claims or any(_number(n) != _number(context.score) for n in numbers):
                raise ValueError("CONTRADICTORY_SCORE")
    if context.observations and not result.observation_explanations:
        raise ValueError("EXPLANATION_REQUIRED")
