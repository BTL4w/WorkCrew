SHELL := /bin/bash

UV := uv

.DEFAULT_GOAL := check

.PHONY: sync lint format-check typecheck test eval check

sync:
	$(UV) sync --locked

lint:
	$(UV) run --locked ruff check src tests

format-check:
	$(UV) run --locked ruff format --check src tests

typecheck:
	$(UV) run --locked pyright

test:
	$(UV) run --locked pytest -m "not live_provider"

eval:
	$(UV) run --locked python -m work_management_ai.evaluation.phase2_multi_agent
	$(UV) run --locked python -m work_management_ai.evaluation.phase3_assignment
	$(UV) run --locked python -m work_management_ai.evaluation.phase4_daily_update
	$(UV) run --locked python -m work_management_ai.evaluation.phase4_risk
	$(UV) run --locked python -m work_management_ai.evaluation.phase4_daily_update_risk

check: lint format-check typecheck test eval
