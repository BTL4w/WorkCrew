# Phase 2 Multi-Agent Assistant runbook

This runbook covers the three Phase 2 activated Agents only: Orchestrator,
Work Intelligence and Planning. The user-owned root `README.md` is not the
runbook target and must not be replaced by this document.

## Local prerequisites

Run every repository command in Ubuntu WSL2 from the canonical checkout. The
local stack requires Docker, Python 3.13 through `uv`, Node 24 and Corepack.

```bash
make install
make db-up
make migrate
make seed
```

The default local login accounts are `manager@example.test` and
`employee@example.test`; use the local demo password configured for the seed.
Do not reuse demo credentials or `.env` files in production.

## Provider modes

Disabled mode is fail-closed and keeps manual Work and Planning available:

```bash
export APP_AI_PROVIDER=disabled
make dev
```

Mock mode is the only mode used by the default automated suite. It requires no
network access or hosted-provider credential:

```bash
export APP_AI_PROVIDER=mock
make dev
```

OpenAI mode is opt-in. Keep the key in the shell environment or an ignored
local secret store; never put it in Git, command output, screenshots or traces.

```bash
export APP_AI_PROVIDER=openai
export APP_AI_MODEL='<approved-model-name>'
export APP_OPENAI_API_KEY='<secret-from-your-local-secret-store>'
make dev
```

Production refuses an incomplete OpenAI configuration. Live-provider tests
remain separate, opt-in and credential-gated.

## Conversation naming

The first accepted text message of an unnamed conversation atomically queues
one `assistant.conversation.title` job alongside the response job. The worker
processes naming independently of Orchestrator execution, without another Agent
or a business-write proposal. Deterministic policy permits this owner-scoped,
low-risk operational metadata update; it grants no business mutation authority.
Explicit titles are preserved. Existing conversations are not backfilled.

Naming uses the same provider/API key but a dedicated model:

```bash
export APP_AI_TITLE_MODEL=gpt-4o-mini
export APP_AI_TITLE_TIMEOUT_SECONDS=3
```

Only the first message (up to 2,000 characters) is sent, with an 80-token output
budget and a 100-character title limit. The prompt asks for 4–6 words in the
message's language. Mock mode returns a deterministic prefix; disabled mode,
timeout and invalid output fall back to the first six words. No later message
triggers renaming. Naming makes no provider retries; database/job failures use
the existing lease/retry mechanism, and a committed title is never overwritten.

The title, safe versioned audit metadata, transactional outbox record and
`assistant.conversation.titled.v1` event commit together. The event carries only
the conversation identifier; the authenticated UI fetches its snapshot and
updates the sidebar. Raw model prompts/results are not stored separately.
The existing external outbox publisher is still unsupported; naming UI delivery
uses the durable Assistant event stream and does not depend on external dispatch.

Apply migration `0017` before running the updated API/worker. Stop old workers
during this rollout: older binaries do not distinguish naming jobs. For an
application rollback, pause the worker until a compatible worker is available;
retain the forward-compatible schema and job evidence. Migration downgrade
fails safely if naming jobs exist rather than deleting them.

Verify with `make lint`, `make typecheck`, `make test`, `make migration-check`,
`make eval` and `make test-e2e`. Live naming quality/latency is credential-gated
and is not measured by mock tests.

## Worker tenant scope

The worker never discovers tenants from a client request. Obtain the seeded
tenant UUID from local PostgreSQL:

```bash
docker compose exec -T postgres \
  psql -At -U work_management -d work_management \
  -c "SELECT id FROM organizations ORDER BY created_at, id LIMIT 1"
```

Set `APP_WORKER_ORGANIZATION_IDS` to a JSON array, not a comma-separated string:

```bash
export APP_WORKER_ORGANIZATION_IDS='["00000000-0000-0000-0000-000000000000"]'
make dev
```

Replace the example UUID with the query result. An empty array deliberately
leaves the worker idle.

## Process boundaries

`make dev` starts three application processes after bootstrap:

- FastAPI serves `/api/v1`, performs authentication/authorization and commits
  short request transactions.
- The worker claims tenant-scoped Assistant, Planning, outbox and projection
  work. Model calls occur after database transactions close.
- Next.js renders the Work UI and conversation-first Assistant. It never owns
  business authorization or writes directly to PostgreSQL.

PostgreSQL is the business and operational source of truth. No broker, Redis,
Qdrant or optional integration is required for this Phase 2 slice.

## Deterministic checks

These commands use mocks and must not require `OPENAI_API_KEY`:

```bash
UV_CACHE_DIR=/tmp/ai-native-work-management-uv-cache make eval
UV_CACHE_DIR=/tmp/ai-native-work-management-uv-cache make test
UV_CACHE_DIR=/tmp/ai-native-work-management-uv-cache make lint
UV_CACHE_DIR=/tmp/ai-native-work-management-uv-cache make typecheck
```

When PostgreSQL is healthy, run the browser and migration gates:

```bash
docker compose ps
UV_CACHE_DIR=/tmp/ai-native-work-management-uv-cache make test-e2e
UV_CACHE_DIR=/tmp/ai-native-work-management-uv-cache make migration-check
```

`make eval` reads the redacted bilingual JSONL suite and prints aggregate
counts only. It does not call a model-as-judge or a hosted provider.

## Troubleshooting conversations

- No response after posting: confirm the worker log says it started, then
  verify `APP_WORKER_ORGANIZATION_IDS` contains the conversation tenant UUID.
- Safe manual fallback: check `APP_AI_PROVIDER`; disabled mode and missing mock
  fixtures fail closed by design. Inspect only safe codes in persisted Agent
  invocation metadata, never raw provider errors or prompts.
- Conversation not visible: history is owner-only. Confirm the signed-in
  membership owns the conversation and belongs to the active organization.
- Repeated response after refresh: inspect durable job, Agent Run, Tool call and
  message dedupe keys before retrying. Do not manually reapply business rows.
- Cross-tenant or forbidden response: current membership role is resolved again
  by the worker. Do not bypass RLS, change the recorded tenant or grant roles
  through a prompt.

## Troubleshooting Planning

- Planning controls are absent for an Employee by policy. A Manager or Admin is
  required for `planning.create`, `planning.resume` and `planning.revise`.
- `NEEDS_INPUT`: answer the inline question in the same conversation. The
  accepted card action supplies the trusted workflow reference.
- Stale proposal: use the newest exact version. Old cards remain read-only and
  edits are revalidated before any decision.
- Proposal exists but no Project exists: expected. Planning creates immutable
  proposal state only. Task 8 approval is the sole path that applies one
  business graph; rejection creates no business rows.
- Projection lag: keep the worker running. Committed focused workflow events
  are projected into inline Assistant blocks without a second model call.

Do not start Phase 3, a future Agent package, personalized memory, autonomous
mutation, a broker or an optional integration from this runbook.

## Phase 2 closure evidence — 2026-08-24

Task 11 closes the Phase 2 slice with deterministic local evidence:

- `make lint` and `make typecheck` pass for Python, TypeScript and the E2E suite.
- `make test` passes 398 backend/AI tests and 95 frontend tests.
- `make migration-check` reaches Alembic `0010` with no schema drift and passes
  all 37 PostgreSQL integration tests.
- `make eval` passes all 24 bilingual cases with zero policy violations,
  unsupported claims or duplicate side effects.
- `make test-e2e` passes all three browser scenarios. The Assistant scenario
  verifies an approved Goal/Milestone/Project Week/Task/dependency/acceptance
  graph, including that every AI-planned Task remains unassigned; it also
  verifies rejection creates no second Project. The manual scenario creates a
  Goal, Milestone, Project Week and assigned Task without posting an Assistant
  mutation while the API remains in its default disabled-provider mode.
- The frontend contract manifest pins Assistant routes, transcript block
  discriminators and conversation request/response schemas to FastAPI OpenAPI.

The default closure suite uses the deterministic mock provider. Hosted-provider
verification remains opt-in and credential-gated; it is not claimed by this
record. Expected local outbox retries remain visible because Phase 2 has no
external publisher, and no optional integration or deployment track is
activated.

## Sidebar conversation management

Apply additive migration `0043` before deploying the updated API. It adds an
unpinned default to existing conversations and grants the runtime update access
to that column. Keep the additive schema when rolling back the application.

The sidebar scrolls overflowing titles inside their row on hover/focus (respecting
reduced-motion preferences). Pinned chats appear above the Recent section;
the three-dot menu supports owner-controlled renaming and confirmed removal.
Chat ordering follows the last transcript message (creation time for empty chats),
so pinning, unpinning and renaming preserve recency.
`PATCH /api/v1/ai/conversations/{id}` accepts `title` (1–120 trimmed characters)
and/or `is_pinned`. `DELETE` archives a chat from the owner's list and public
snapshot/event endpoints; confirmed business records and required audit remain.
Deletion returns a conflict while an assistant turn is queued/running.

Both mutations require `Idempotency-Key` and `If-Match` using the conversation's
`version`. They enforce authenticated owner/tenant context, RLS, a resource lock,
version validation, transaction, audit and `assistant.conversation.updated.v1`
event/outbox. Unknown, foreign and already-archived resources return a
non-disclosing 404; matching retries replay the original response. The client
retains the original key/version after uncertain failures, including across
operations on different chats. Reload to reconcile a stale version before
submitting a new attempt.

Verify the UI with `frontend/e2e/conversation-management.spec.ts`; it creates its
own empty conversation, checks persisted pins/renaming, and archives only that
conversation. It makes no model calls. Repository/API isolation, stale writes,
replay, busy-turn rejection and audit/outbox checks are in
`backend/tests/test_assistant_repository_integration.py`.
