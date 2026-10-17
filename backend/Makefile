SHELL := /bin/bash

UV := uv
LOCAL_ENV := $(if $(wildcard ../.env),--env-file ../.env,)

.DEFAULT_GOAL := up

.PHONY: up dev sync migrate seed lint format-check typecheck test test-ai test-integration migration-check worker-dev

up: sync
	$(UV) run $(LOCAL_ENV) fastapi dev app/main.py

dev: up

worker-dev: sync
	$(UV) run $(LOCAL_ENV) python -m app.worker

sync:
	$(UV) sync --locked

migrate: sync
	$(UV) run alembic upgrade head

seed: sync
	APP_DEMO_SEED_ENABLED=true $(UV) run python -m app.scripts.seed_demo

lint:
	$(UV) run ruff check app tests alembic ../ai/src ../ai/tests

format-check:
	$(UV) run ruff format --check app tests alembic ../ai/src ../ai/tests

typecheck:
	$(UV) run pyright

test:
	$(UV) run pytest -m "not integration and not live_provider"

test-ai:
	$(UV) run pytest ../ai/tests ../ai/src/work_management_ai/agents -m "not live_provider"

test-integration:
	RUN_POSTGRES_INTEGRATION=1 $(UV) run pytest -m integration

migration-check: migrate
	$(UV) run alembic current --check-heads
	$(UV) run alembic check
	$(MAKE) test-integration
