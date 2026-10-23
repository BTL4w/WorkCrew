SHELL := /bin/bash

UV := uv

.DEFAULT_GOAL := check

.PHONY: sync lint format-check typecheck test eval eval-retrieve check

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
	$(UV) run --locked python -m work_management_ai.evaluation.core_mvp

eval-retrieve:
	$(UV) run --locked python -m work_management_ai.evaluation.retrieve_web

check: lint format-check typecheck test eval eval-retrieve
