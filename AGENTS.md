# AGENTS.md

## Purpose

This repository contains an AI-native, cross-domain enterprise work-management
platform built around one conversation-first, hub-and-spoke multi-agent system.
Work must follow the solo-developer vertical-slice sequence in `PLAN.md`.

The current repository is in the planning/documentation stage. Do not write application code until the user explicitly asks to implement a named phase or a clearly bounded item from `PLAN.md`.

## Required Reading and Precedence

Before changing the repository, read:

1. `AGENTS.md` for execution rules.
2. `PLAN.md` for implementation order, phase scope and Definition of Done.
3. `AI_Native_Work_Management_System_Description.md` for product intent and long-term architecture.

Use this precedence when instructions appear to conflict:

1. The user's current request.
2. `AGENTS.md` safety and execution rules.
3. The active phase in `PLAN.md`.
4. The system description.

`PLAN.md` intentionally does not repeat the entire architecture description. Do not treat omitted future capabilities as authorization to add them early.

## Phase Discipline

- Implement only one vertical slice at a time.
- Start a phase only when the user explicitly requests it.
- Do not implement a later phase to “prepare” for it unless the current phase requires a minimal interface boundary.
- Every slice must include its necessary frontend, backend, database migration, authorization, audit and tests.
- Keep changes small enough for a solo developer to understand, demo and revert.
- Satisfy the phase's Definition of Done before moving to the next phase.
- Respect every Explicit non-goal in the active phase.
- Do not start Optional Integrations or the Deployment Track until the Core MVP Exit Gate passes.
- Do not start an Advanced Track item until its activation gate and benchmark requirements pass.

Core MVP order is fixed:

1. Manual Project/Task Core.
2. AI Planning Proposal plus goal, milestone, dependency and acceptance criteria.
3. Skills, capacity, deterministic assignee ranking and AI explanation.
4. Manual and AI daily update, blocker and AI-assessed risk.
5. Management report, feedback and evaluation loop.

Google Calendar and Qdrant are Post-MVP Optional Integrations. Kubernetes/kind, Jenkins and GKE are Post-MVP deployment work. They must not leak into Core MVP implementation.

## Architecture Guardrails

- Use a FastAPI modular monolith and one worker that imports the same domain/application packages.
- Do not create a microservice for each module.
- Keep domain modules independent of web frameworks, model-provider SDKs and external integration SDKs where practical.
- Use application services for use cases and transactions.
- External adapters implement typed ports owned by the application/domain side.
- PostgreSQL is the business source of truth.
- Redis may hold cache, locks, rate limits and short-lived state only.
- Qdrant, when its optional phase is authorized, is a retrieval index and never a source of truth.
- Store the Work Graph through relational foreign keys and relation tables. Do not introduce a graph database in the MVP.
- Do not introduce GraphRAG for direct lookup or simple retrieval.
- Use one bounded hub-and-spoke Agent Runtime: an Orchestrator Agent may delegate
  to phase-activated Specialist Agents through typed handoffs. Specialists must
  not call each other directly.
- Do not create an unrestricted swarm, peer-to-peer delegation, self-created
  agents or a separately named agent for every endpoint/feature. A Specialist
  Agent is justified only by an independently testable capability, context,
  skill/tool, workflow and evaluation boundary recorded in `PLAN.md`.
- During Core MVP, all agents run inside the same application/worker runtime.
  Do not deploy a microservice or broker per agent.
- Keep the product domain-neutral. Do not make IT, software-development, repository, pull-request or CI/CD concepts part of core business behavior.

## Project Structure

When Phase 1 is authorized, use these top-level boundaries unless the user approves a change:

- `frontend/` — Next.js, React and TypeScript UI.
- `backend/app/` — FastAPI entrypoint, domain modules, application services and adapters.
- `backend/alembic/` — PostgreSQL migrations and RLS policies.
- `ai/` — Model Gateway, Agent Runtime, Orchestrator/Specialist packages,
  manifests, graphs, skills, tools, evaluators and safe traces from Phase 2.
- `tests/` or colocated test directories consistent with the selected framework.
- `deploy/` — created only when the Post-MVP Deployment Track is authorized.

Avoid placeholder packages for future phases. Create a module when the active vertical slice first needs it.

## API and Event Contracts

- Put every product API under `/api/v1`.
- Keep REST/OpenAPI as the primary application interface.
- Use SSE only for one-way workflow progress or notifications when required.
- Validate request and response bodies with typed schemas.
- Use one structured error contract and never expose internal stack traces to clients.
- Require idempotency keys for retryable state-changing operations and external side effects.
- Use optimistic concurrency or an equivalent resource version for proposal approval and stale-sensitive mutations.
- Version domain event envelopes and write events through a transactional outbox.
- Do not silently break a public API or event contract. Update schema, clients and contract tests together.

## Data and Tenant Isolation

- Every tenant-owned row must have a non-null `organization_id`.
- Include `organization_id` in tenant-owned indexes and unique constraints.
- Ensure references between tenant-owned records cannot cross organizations.
- Enforce PostgreSQL Row-Level Security in addition to application authorization.
- Application and worker roles must not use `BYPASSRLS`.
- Establish tenant context for every request, transaction, job and outbox consumer.
- Include tenant context in cache keys, job payloads, vector payloads and object-storage keys.
- Never trust an arbitrary organization identifier sent by the client; resolve allowed tenant context from authenticated membership.
- Add negative cross-tenant tests for each new tenant-owned resource.
- Chat history, model context, vector records and temporary workflow memory are not official business facts.
- Conversation, orchestration, agent-run, handoff, skill/tool invocation and
  checkpoint rows are tenant-owned operational state, not authorization facts.

## Authorization, Approval and Audit

Manual Manager actions are not proposals by default:

- An authorized manual Manager write proceeds directly through validation and transaction boundaries.
- Every successful or rejected sensitive mutation must leave the required audit evidence.
- Do not create approval friction for ordinary manual project, task, assignment, update or report actions unless policy marks them high risk.

Approval is mandatory for:

- AI-proposed writes.
- External side effects.
- Bulk changes.
- High-risk actions.
- Any action explicitly required by an organization policy.

Human gates are role- and ownership-aware:

- A user confirms an AI-extracted draft of their own Daily Update before it is
  persisted.
- A Manager/Admin approves AI-proposed plan, assignment, deadline, dependency,
  bulk or organization-level changes.
- Read-only answers and verified analysis do not require approval.
- An explicit low-risk, user-owned, reversible action may execute directly only
  when deterministic policy permits it; AI-inferred actions remain proposals.

For an approved side effect, follow:

```text
intent
→ entity resolution
→ authorization and RLS
→ policy
→ validation
→ simulation or diff
→ approval when required
→ idempotent transaction
→ outbox
→ audit
```

- AI cannot approve its own output.
- Employees cannot gain Manager privileges through AI or tool calls.
- Rejecting a proposal must create no business side effect.
- Edited or stale proposals must be revalidated before execution.
- Use compensation or a documented recovery path when an external side effect cannot be transactionally rolled back.

## AI, Workflow, Skill and Tool Rules

- Use a provider-neutral Model Gateway.
- Use an OpenAI hosted API for MVP production-quality calls and a deterministic mock provider for local and automated tests.
- Do not scatter provider SDK calls through domain modules or workflow nodes; route them through the gateway.
- Use typed structured output for every model call that affects product behavior.
- Use deterministic code for authorization, business invariants, arithmetic, dates, workload, Phase 3 ranking, constraints and post-condition verification.
- The user-approved Phase 4 AI-first exception lets LLMs author evidence-support and contextual risk scores, rationale and advisory recommendations. Code validates typed output, source/tenant/version provenance and score range, applies thresholds and preserves human gates; it must not replace those model scores with fixed factor arithmetic.
- LLMs may understand requests, prepare drafts and explain results. They may not override deterministic authorization, business invariants, Phase 3 ranking or approval decisions.
- Use one Orchestrator Agent as the only component allowed to create a typed
  handoff to a Specialist Agent. Every specialist result returns to the
  Orchestrator; no direct specialist-to-specialist delegation is allowed.
- Each implemented agent package must include `agent.yaml`, typed
  `contracts.py`, `harness.py`, versioned prompts, workflows/graphs as needed,
  allowed skills, evaluators and tests. The manifest declares agent/version,
  owner, activation phase, capabilities, permissions, risk ceiling, model
  policy, skill/tool allowlists, budgets, approval rules, fallbacks and stop
  conditions.
- Agent Registry must reject inactive, unknown, invalid or permission-incompatible
  agents. Do not create placeholder packages for future-phase agents.
- An Assistant Turn owns one durable Orchestration Run and may create multiple
  bounded Agent Runs. Persist typed execution plans, handoffs, checkpoints,
  evidence and safe decisions; never persist or expose hidden chain-of-thought.
- The Agent Harness surrounds every model/tool loop with manifest loading,
  context construction, policy guards, budgets, retries, checkpoints,
  verification, safe tracing and manual fallback.
- Agents may reason, plan, re-plan and call tools only within declared budgets
  and permissions. Models cannot grant roles, tenant scope, approval state,
  tools, skills or activation status.
- Every AI-generated business mutation remains a proposal until the appropriate
  human gate succeeds. Owner-confirmed drafts and policy-authorized explicit
  low-risk user actions follow their separately documented path.
- Every workflow must define typed state, nodes, edges, retry limits, stop conditions, approval points, verifiers and fallback behavior.
- Persist only structured orchestration/workflow state required for execution
  and audit. Do not persist hidden chain-of-thought.
- Record agent, manifest, handoff, workflow, skill, tool, prompt, model and
  verifier versions with each applicable run.
- A tool must have typed input/output, tenant scope, permission, risk level, timeout, retry policy, idempotency behavior and audit behavior.
- Tools call application services; they do not write directly to the database.
- Skills must declare trigger, input/output schema, required context, allowed tools, risk/approval rules, owner, semantic version and evaluation cases.
- Load skills progressively. A skill is reusable capability/instruction, not an
  Agent; a Tool is a typed action, not a source of authorization.
- Load only context that the current node needs and preserve source, tenant, permission, version and timestamp provenance.
- Core MVP memory is limited to structured working memory and conversation
  memory. Personalized long-term memory is an optional later extension and must
  not be implemented without its Settings, retention, inspection/deletion and
  consent design.
- A failed model or verifier must fall back to the manual product flow where that flow is essential.

## Model Data and Evaluation

- Do not fine-tune or distill a model during Core MVP.
- Do not use raw production data for training.
- Store raw AI prompt/context for no more than 30 days under the current baseline.
- Store redacted traces for no more than 90 days under the current baseline.
- Do not delete required business audit records when AI context expires.
- Evaluation examples must be permission-safe, redacted, deduplicated and provenance-linked.
- Keep training, held-out evaluation and production feedback datasets separate.
- A model change must pass the relevant golden suite before promotion.
- Self-hosted OpenAI-compatible inference and distillation require the activation gate in `PLAN.md`, offline evaluation, shadow testing and canary promotion.
- GraphRAG requires its own benchmark and Definition of Done. Technology availability is not an activation reason.

## Frontend Rules

- Keep Manager and Employee flows usable without chat.
- Treat chat as a command center, not the only interface.
- Show structured proposals, validation results, evidence, assumptions and before/after differences rather than relying on prose alone.
- Always provide an edit/reject path before an AI write is approved.
- Display unknown, stale or unavailable data explicitly.
- Support Vietnamese and English from the start through translation keys; do not hard-code business UI text throughout components.
- Do not display a confidence value without explaining what it represents and where it came from.

## Backend and Database Rules

- Keep business invariants in domain/application code, not prompts or frontend-only checks.
- Keep transactions inside application-service boundaries.
- Use Alembic for every schema change.
- Prefer forward-compatible migrations and explicit backfills.
- Do not combine a destructive migration with an application behavior switch unless a safe rollout plan is documented.
- Keep accurate calculations and report metrics in deterministic queries/services.
- An LLM-generated report narrative may only use a verified immutable metric snapshot.
- Store audit events, approvals and outbox events append-only except for documented retention behavior.

## Tests and Global Definition of Done

For every applicable change:

- Run formatting/lint checks, type checks, unit tests and integration tests.
- Add an end-to-end test for the phase's primary user-visible flow.
- Test authorization, RLS, audit and idempotency for each mutation.
- Test AI success, invalid structured output, provider timeout, verifier rejection and manual fallback paths.
- Use mock model and integration adapters in the default automated suite.
- Keep live-provider tests separate, opt-in and credential-gated.
- Add bilingual evaluation cases for user-facing AI workflows.
- Test agent manifests/contracts, Orchestrator execution plans, typed handoffs,
  inactive-agent denial, specialist isolation, tool allowlists, bounded loops,
  checkpoint recovery and multi-agent integration for every activated agent.
- Approval bypass, unauthorized delegation, peer-to-peer handoff and
  cross-tenant leakage test counts must remain zero.
- Verify migrations and public OpenAPI contracts.
- Update local run/demo instructions when commands or dependencies change.
- Confirm that no Explicit non-goal was introduced.

A task is not complete merely because the happy path works. It is complete only when the applicable Global Definition of Done in `PLAN.md` passes.

## Local Commands

### Accepted local environment boundary

- Run Codex and all repository Git, search, edit, Make, Python, Node, pnpm,
  Docker and test commands inside Ubuntu WSL2.
- Use the canonical checkout at `/home/btl4w/code/ai-native-work-management`.
- Use Windows PowerShell only for host-level operations such as backup or WSL
  management, not for repository commands.
- Never share or reuse `.venv` or `node_modules` between Windows and Ubuntu.

When Phase 1 creates the project tooling, expose stable repository-level commands for at least:

- `make dev`
- `make lint`
- `make typecheck`
- `make test`
- `make migration-check`
- `make eval` once AI evaluation exists
- `make kind-test` only after the Kubernetes deployment track begins

Until those commands exist, inspect the actual project manifests and use their documented commands. Do not invent successful test results.

## Change and Git Discipline

- Inspect `git status` before editing and preserve unrelated user changes.
- Do not rewrite, delete or revert user-owned changes unless explicitly asked.
- Do not use destructive Git or filesystem commands without explicit authorization.
- Do not commit, push, create a pull request or modify remote state unless the user explicitly requests it.
- Do not include secrets, tokens, credentials, private prompt traces or sensitive datasets in Git.
- Report files changed, tests run and any unverified behavior at handoff.

## Plan Maintenance

- If implementation reveals a conflict with `PLAN.md`, stop and report the concrete conflict before expanding scope.
- Update `PLAN.md` only when the user asks for a plan change or when an authorized implementation task explicitly includes plan-status maintenance.
- Do not mark a phase complete until its full Definition of Done passes.
- Record unresolved architecture or governance decisions instead of silently choosing an option that materially changes security, tenant isolation, deployment or AI data handling.
