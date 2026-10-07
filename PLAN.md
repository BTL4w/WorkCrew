# Solo Developer Execution Plan

## 1. Execution Rules

This plan is an implementation sequence, not a restatement of the full architecture. Product intent and long-term architecture remain documented in `AI_Native_Work_Management_System_Description.md`.

The implementation rules are:

- Build one vertical slice at a time. Every phase must produce an independently demonstrable user outcome.
- Complete and stabilize Core MVP Phases 1–5 before starting optional integrations or the deployment track.
- Keep the runtime as a FastAPI modular monolith with one worker using the same
  business-domain and AI packages. A separate source module does not imply a
  separately deployed AI microservice.
- Keep PostgreSQL as the source of truth. Chat state, model context and retrieval indexes are never authoritative business state.
- Expose AI through one full-page, conversation-first Assistant available to
  every authenticated role. One Orchestrator Agent understands the objective,
  creates a bounded multi-step execution plan and delegates through typed
  handoffs to phase-activated Specialist Agents; users never select an internal
  agent, workflow, Skill or Tool manually.
- Use a hub-and-spoke topology. Specialist Agents return typed results or
  requested handoffs to the Orchestrator and never call one another directly.
  Run all Core MVP agents in the same AI runtime and worker; do not create an
  agent microservice or broker per capability.
- Keep capability and authorization boundaries role-aware. A shared chat shell
  does not grant every role the same tools, data or mutation permissions.
- Place every HTTP API under `/api/v1`.
- Manager actions performed manually are written directly after authorization, validation and audit. They do not require an approval record by default.
- Approval is mandatory for AI-proposed organization writes, external side
  effects, bulk changes and high-risk actions. An owner must confirm an
  AI-extracted draft such as their own Daily Update before persistence.
- Explicit low-risk, user-owned, reversible actions may execute directly only
  when deterministic policy permits them. AI-inferred mutations remain
  proposals and no Agent may approve its own output.
- Use deterministic code for authorization, business invariants, arithmetic, dates, workload, Phase 3 ranking and verification. User-approved Phase 4 AI-first evidence/risk assessments are the exception: LLMs author contextual scores, rationale and advisory recommendations; code validates output/provenance and applies thresholds, without recomputing scores or granting mutation authority.
- Use a provider-neutral Model Gateway. The MVP uses an OpenAI hosted API plus a deterministic mock provider for local and automated tests.
- Do not add Calendar, Qdrant, Kubernetes, Jenkins, GKE, self-hosting, distillation or GraphRAG to a Core MVP phase.

### 1.1. Repository ownership and AI engineering emphasis

The repository has three primary product-code boundaries:

- `backend/` owns FastAPI transport, business domain/application services,
  authorization, transactions, PostgreSQL adapters, migrations and audit.
- `ai/` is the first-class AI engineering module. From Phase 2 onward it owns
  the Model Gateway, shared Agent Runtime, Agent/Skill/Tool Registries,
  Orchestrator and Specialist packages, versioned manifests/prompts, structured
  contracts, workflow graphs, skills, AI tools, verifiers, safe traces and
  evaluation.
- `frontend/` is the supporting product/demo surface for manual flows, structured
  AI proposals, evidence, approval and feedback.

System operations remain intentionally small: root Docker Compose supports local
Core MVP development; `deploy/` is still forbidden until the Core MVP Exit Gate.

`ai/` is a separate Python package boundary in the same repository and runtime.
Local development gives `backend/` and `ai/` separate `.venv` directories so
each package proves its declared dependencies independently. `ai/uv.lock` owns
the isolated AI quality environment; `backend/uv.lock` remains the integrated
runtime lock and installs `ai/` as an editable package for API/worker integration
tests. Dependency and execution rules are:

```text
FastAPI composition root / worker
→ Orchestrator Agent → phase-activated Specialist Agent
→ Agent graph / Skill / typed Tool
→ typed application ports
→ deterministic backend services and external adapters
```

- Backend domain code must never import model-provider, agent-framework or AI
  workflow implementations.
- AI tools call typed backend application services; they never write PostgreSQL
  directly or bypass authorization, approval, tenant context and audit.
- The API process and worker may import `ai/`; `ai/` is not deployed as an
  independent network service during Core MVP.
- Each activated Agent is an independently versioned runtime package with
  `agent.yaml`, typed contracts, Harness, prompts, graph/skills, evaluators and
  tests. A source boundary does not imply a process boundary.
- Do not create `ai/` during Phase 1. Introduce it only when Phase 2 activates
  the first evaluated AI workflow.

Local Docker builds preserve the same boundaries without introducing a Core MVP
microservice:

- `frontend` is an independently built Next.js container.
- `backend-api` is an independently built FastAPI container on port `8000`.
- Before the Task 5 worker exists, the independently built AI image is a
  short-lived `ai-check` quality container only; it must not run a placeholder
  daemon or expose a network port.
- Task 5 changes that image's runtime role to `ai-worker`, which runs the single
  PostgreSQL-backed worker entrypoint, imports the integrated backend/AI packages
  and exposes no HTTP port.
- Container files and local Compose commands are development boundaries, not the
  Post-MVP Deployment Track. Do not create `deploy/` or production deployment
  automation here.

## 2. Core MVP

### Phase 1 — Manual Project/Task Core

> **Status: COMPLETE — 2026-08-01.** The Phase 1 outcome and all applicable
> Global Definition of Done gates have been verified. Phase 2 remains inactive
> until it is explicitly requested.

#### User-visible outcome

A Manager can sign in, create a project, create and assign tasks, and update project/task details directly. An Employee can see assigned tasks and update their status. This phase is a usable manual task manager without AI.

#### Scope

- Initialize the Next.js frontend, FastAPI backend, PostgreSQL migrations and local Docker Compose environment.
- Implement local database-backed authentication for development and portfolio
  demonstration, with seeded Admin, Manager and Employee accounts. Store only
  password hashes and keep the authentication adapter boundary replaceable.
- Establish Organization membership and the Manager/Employee authorization
  roles. In Phase 1, the seeded Admin persona has Manager-equivalent work
  permissions and no separate administration UI.
- Implement Project and Task CRUD, task assignment and a small fixed task-status workflow.
- Provide Manager project/task views and an Employee “My Tasks” view.
- Enforce tenant context, authorization, Row-Level Security and audit logging.
- Write manual Manager changes directly after validation; do not create unnecessary approval requests.

#### Main files/modules

- `frontend/`: application shell, authentication, projects, tasks and My Tasks features.
- `backend/app/modules/identity` and `backend/app/modules/organization`.
- `backend/app/modules/work`: Project, Task and status behavior.
- `backend/alembic/`: schema, indexes and RLS policies.
- No `ai/` package yet; Phase 1 proves the manual business and governance path
  that later AI tools must reuse.

#### Database and API

Database entities:

- `organizations`
- `users`
- `memberships`
- `auth_sessions`
- `projects`
- `tasks`
- `task_status_transitions`
- `audit_events`
- `idempotency_records`

Required APIs:

- `POST /api/v1/auth/login`
- `POST /api/v1/auth/logout`
- `GET /api/v1/me`
- `GET /api/v1/members`
- `GET /api/v1/projects`
- `POST /api/v1/projects`
- `GET /api/v1/projects/{project_id}`
- `PATCH /api/v1/projects/{project_id}`
- `GET /api/v1/tasks`
- `POST /api/v1/tasks`
- `GET /api/v1/tasks/{task_id}`
- `PATCH /api/v1/tasks/{task_id}`
- `POST /api/v1/tasks/{task_id}/status`
- `GET /api/v1/my-tasks`

Every tenant-owned table, index, unique constraint and background payload must include tenant context. Application database roles must not use `BYPASSRLS`.

#### Tests

- Project, task, assignment and status domain tests.
- Local login, invalid credential, logout and session tests.
- Admin/Manager/Employee API authorization matrix.
- Cross-tenant API and PostgreSQL RLS negative tests.
- Migration verification.
- Audit coverage and idempotency tests for mutations.
- Frontend component tests and one end-to-end Manager-to-Employee task flow.

#### Definition of Done

- A Manager can create a project and assign a task through the UI.
- An Employee can see only permitted tasks and update status.
- A manual Manager mutation succeeds without an approval record and always creates an audit event.
- Cross-tenant access is blocked at both API and database layers.
- The full phase runs locally with seeded demo accounts and without an external
  identity provider, AI credential or Google credential.

#### Explicit non-goals

- Goal, milestone, dependency and acceptance criteria.
- Skills, capacity, workload or recommendation.
- Daily updates, blocker, risk, reports or feedback.
- Agent Harness, Model Gateway and LLM calls.
- Public registration, account recovery, OIDC/SSO, MFA and user-administration UI.
- Calendar, retrieval infrastructure and deployment automation.

#### Closure record — 2026-08-01

Phase 1 was closed after the following evidence passed against the completed
manual vertical slice:

- `make lint`: Ruff and ESLint passed.
- `make typecheck`: Pyright reported zero errors/warnings and TypeScript passed.
- `make test`: 54 backend unit tests and 33 frontend component/contract tests
  passed.
- `make migration-check`: PostgreSQL reached Alembic revision `0004` (head),
  autogenerate detected no schema drift, and all 10 integration tests passed.
- `make test-e2e`: the production Next.js build passed and both Playwright
  scenarios passed, including the Manager-to-Employee assignment/completion
  flow and invalid-login recovery.
- Project and Task integration coverage verifies role authorization, rejected
  mutations, audit evidence, idempotent replay, optimistic concurrency and
  cross-tenant denial at the API boundary.
- Schema integration coverage verifies RLS default-deny, FORCE RLS,
  tenant-qualified references and cross-tenant denial in PostgreSQL.
- The seeded local demo runs without external identity, AI or Google
  credentials. No `ai/`, Calendar, Qdrant, deployment automation or other
  Phase 1 Explicit non-goal was introduced.

This closes Phase 1 only. It does not activate Phase 2 or the Core MVP Exit Gate.

### Phase 2 — Conversation-First AI Assistant and Weekly Planning Proposal

> **Status: COMPLETE — 2026-08-24.** The Phase 2 outcome and applicable Global
> Definition of Done passed with the deterministic mock provider. Task 11
> closure evidence is recorded below; hosted-provider tests remain separate,
> opt-in and credential-gated.

#### User-visible outcome

A full-page AI Assistant is available to every authenticated user as a normal
conversation. An Employee can ask read-only questions about permitted Projects,
assigned Tasks, status, deadlines, dependencies and Acceptance Criteria. A
Manager can additionally enter a natural-language goal, receive an editable
weekly Project/Task proposal inline in the conversation, and approve or reject
it before any AI-proposed business records are written. Planned Tasks are
organized into explicit Project Weeks and remain unassigned when the plan is
approved. Behind the same chat, a
real hub-and-spoke multi-agent runtime uses an Orchestrator Agent, Work
Intelligence Agent and Planning Agent with independent manifests, contracts,
Harnesses, graphs/skills, tools and evaluators. Manual planning remains available
outside chat.

#### Scope

- Add Goal, Milestone, Project Week, Task Dependency and Acceptance Criteria to
  the manual work-management UI and domain.
- Treat Project Week as a first-class planning entity with a stable week number,
  date range, objective and lifecycle. Every Task created from an approved plan
  belongs to one Project Week.
- Allow an approved plan to create unassigned Tasks. Planning captures required
  skills and estimated effort but does not inspect Employees, recommend a
  Project Team or assign a Task.
- Protect completed Project Weeks as historical facts. Manual or AI re-planning
  may add corrective work only to the active or a future week.
- Manual Manager changes use the direct authorized/audited write path.
- Add the provider-neutral Model Gateway with OpenAI hosted and mock providers.
- Add tenant-owned conversations, immutable user/assistant messages and durable
  Assistant Turns. Each accepted turn creates one durable Orchestration Run and
  may create multiple bounded Agent Runs and child Workflow Runs.
- Add the shared Agent Runtime: versioned manifest loader, Agent/Skill/Tool
  Registries, Model Gateway integration, Context Manager, Policy Guard,
  Execution Engine, budgets, checkpoints, safe tracing and evaluators.
- Implement one Orchestrator Agent as the only delegation authority. It creates
  a typed execution plan, delegates through typed handoffs, observes results,
  performs bounded re-planning, synthesizes the answer and selects a response or
  human gate.
- Implement a read-only Work Intelligence Agent for permission-safe Project/Task
  Q&A and a Planning Agent that owns the goal-to-weekly-project-plan graph,
  immutable proposal creation/revision, deterministic verification and manual
  fallback. The Planning Agent has no Employee skill, capacity, Project Team or
  assignment tools.
- Give every activated Agent its own `agent.yaml`, `contracts.py`, `harness.py`,
  versioned prompts, workflows/skills, evaluators and tests. Specialists never
  call one another directly.
- Return an explicit safe availability response for Assignment, Daily Update,
  Risk, Reporting, Task Execution and other agents whose activation phase has
  not begun.
- Reuse Phase 1 application services for approved Project/Task writes, extended
  through the active phase's application boundaries so Project Weeks and
  unassigned planned Tasks apply atomically.
- Render questions, progress, assumptions, validation errors, before/after
  content, evidence, sanitized Agent activity and approval actions as assistant
  messages and structured cards in the chat transcript.
- Return an editable manual form when the model or verifier fails.
- Limit Core memory to durable working/checkpoint state and conversation memory;
  personalized long-term memory remains an optional later extension.

#### Main files/modules

- Frontend `planning`, `ai_assistant` and reusable assistant-card features.
- Backend `assistant` module for conversation/message/turn, Orchestration/Agent
  Run, handoff/job/checkpoint persistence, authorization, idempotency and
  conversation event streaming.
- Backend `work/planning` domain additions, proposal persistence and approval
  application services.
- `ai/model_gateway`: provider-neutral contracts plus hosted OpenAI and
  deterministic mock adapters.
- `ai/runtime`: Agent/Skill/Tool Registries, typed handoffs, policy/context
  guards, execution engine, checkpoint interfaces and safe tracing.
- `ai/agents/orchestrator`: manifest, contracts, Harness, prompt, orchestration
  graph, evaluator and tests.
- `ai/agents/work_intelligence`: manifest, contracts, read-only Harness, Work
  skills/tools, grounding evaluator and tests.
- `ai/agents/planning`: manifest, contracts, Harness and the existing typed
  planning graph, prompts, verifier, checkpoint/manual fallback and tests.
- `ai/skills` and `ai/tools`: progressively loaded, versioned reusable
  capabilities and typed application-service adapters.
- `ai/evaluation`: per-Agent and multi-agent golden cases, malformed-output,
  timeout, injection, delegation and policy evaluation fixtures.

#### Database and API

Database entities:

- `assistant_conversations`
- `assistant_messages`
- `assistant_turns`
- `assistant_events`
- `orchestration_runs`
- `agent_runs`
- `agent_handoffs`
- `agent_checkpoints`
- `assistant_jobs`
- `skill_invocations`
- `tool_invocations`
- `goals`
- `milestones`
- `project_weeks`
- `task_dependencies`
- `acceptance_criteria`
- `proposals`
- `approvals`
- `workflow_runs`
- `workflow_checkpoints`
- `model_invocations`
- `context_references`

Required APIs:

- `POST /api/v1/ai/conversations`
- `GET /api/v1/ai/conversations`
- `GET /api/v1/ai/conversations/{conversation_id}`
- `POST /api/v1/ai/conversations/{conversation_id}/messages`
- `GET /api/v1/ai/conversations/{conversation_id}/events`
- CRUD under `/api/v1/goals`
- CRUD under `/api/v1/milestones`
- CRUD under `/api/v1/projects/{project_id}/weeks`
- CRUD under `/api/v1/task-dependencies`
- CRUD under `/api/v1/acceptance-criteria`
- `POST /api/v1/ai/planning-runs`
- `GET /api/v1/workflow-runs/{run_id}`
- `GET /api/v1/workflow-runs/{run_id}/events`
- `PATCH /api/v1/proposals/{proposal_id}`
- `POST /api/v1/approvals/{approval_id}/decision`

#### Tests

- Conversation/message/turn lifecycle, ordering, idempotency and SSE replay.
- Agent manifest/Registry validation, phase activation and tool/skill allowlists.
- Orchestrator single- and multi-intent planning, typed handoffs, bounded
  delegation/re-planning and unavailable future agents.
- Employee read-only Task/Project Q&A with negative cross-tenant and
  unauthorized-field cases.
- Manual goal/milestone/Project Week/dependency/criteria CRUD and authorization.
- Project Week numbering, non-overlapping date-range, lifecycle and
  completed-week immutability tests.
- Dependency-cycle, Project Week/Task tenant and date-invariant tests.
- Model Gateway adapter contract tests using the mock provider.
- Planning workflow node, branch, pause and resume tests.
- Multi-agent checkpoint/crash recovery and proof that a retry does not duplicate
  Agent Runs, handoffs, tool calls, Workflow Runs or proposals.
- Structured-output and proposal-schema validation.
- Proof that Planning never loads Employee recommendation context and that plan
  approval creates every planned Task unassigned in its exact Project Week.
- Exact-version weekly re-planning tests covering urgent-request diffs, moved
  Tasks, unchanged existing assignments, unassigned new Tasks and zero partial
  state after a stale or concurrent revision.
- Approve, edit, reject, stale proposal and idempotent replay tests.
- Bilingual golden cases and prompt-injection cases.

#### Definition of Done

- Every authenticated role can open the same full-page Assistant, create or
  resume a conversation and receive an inline assistant response without
  selecting an Agent, workflow, Skill or Tool.
- Orchestrator, Work Intelligence and Planning are three independently versioned
  Agent packages with manifests, contracts, isolated Harness/tool boundaries,
  evaluators and tests; Phase 2 is not implemented as one renamed router.
- One turn can delegate to Work Intelligence and Planning through the
  Orchestrator, and no Specialist Agent can delegate directly to another.
- Employee Q&A returns only permitted existing Project/Task facts and cannot
  mutate Project, plan, assignment or approval state.
- A planning turn appears as messages and inline cards in the conversation;
  workflow progress does not replace the transcript with a planning dashboard.
- A planning proposal shows explicit Project Weeks and every planned Task's week.
- Plan approval creates the complete Goal/Milestone/Project Week/Task graph with
  all newly planned Tasks unassigned.
- Planning never recommends a Project Team or Task assignee. Project Team
  recommendation remains unavailable until Phase 3.
- Completed Project Weeks cannot be rewritten; corrective work is recorded in
  the active or a future week.
- Manual planning remains fully usable when the model provider is unavailable.
- No Project, Milestone or Task is created from AI output before Manager approval.
- Approval commits the proposal once through existing domain services and creates audit/outbox records.
- Rejection has no business side effect.
- Every applicable run records agent/manifest, handoff, workflow, skill, tool,
  model, prompt and verifier versions without storing chain-of-thought.
- Invalid output falls back to an editable manual planning form.

#### Closure record — 2026-08-24

- `make lint` and `make typecheck` passed for backend, AI, frontend and E2E code.
- `make test` passed 398 backend/AI tests and 95 frontend tests.
- `make migration-check` reached Alembic revision `0010` with no schema drift
  and passed all 37 PostgreSQL integration tests.
- `make eval` passed all 24 bilingual cases with zero policy violations,
  unsupported claims or duplicate side effects.
- `make test-e2e` passed all three browser scenarios. Acceptance coverage proves
  approval creates the exact Goal/Milestone/Project Week/Task/dependency/
  acceptance-criteria graph with every AI-planned Task unassigned, rejection
  creates no second Project, and manual Goal/Milestone/Project Week/Task work
  remains available without posting an Assistant mutation in disabled-provider
  API mode.
- The frontend Assistant manifest now checks route methods, transcript block
  discriminators and conversation request/response schemas against FastAPI
  OpenAPI.
- The closure introduced no Phase 3 runtime package, assignment recommendation,
  optional integration, deployment automation, microservice split, long-term
  memory or hidden chain-of-thought persistence.

This closes Phase 2 only. It does not activate Phase 3, an Optional Integration,
the Deployment Track or any Advanced Track item.

#### Explicit non-goals

- Employee skills, capacity, Project Team recommendation and Task assignment
  through the Assistant.
- Persisting AI-extracted daily updates, risk or report generation. The shared
  Orchestrator may identify these intents but must return a safe availability card
  until their owning phase is activated.
- Assignment, Daily Update, Risk, Reporting or Task Execution Agent packages at
  runtime before their owning activation phase.
- Personalized long-term memory.
- Peer-to-peer specialist calls, self-created agents, an unrestricted swarm,
  autonomous plan mutation or an Agent microservice per capability.
- Qdrant, GraphRAG and document ingestion.
- Calendar integration.
- Self-hosted inference or training.

### Phase 3 — Skills, Capacity, Project Team and Explicit Assignment

#### User-visible outcome

A Manager can maintain Employee skills, capacity and leave, inspect workload,
and request a ranked Project Team recommendation. Ranking is deterministic; AI
produces only an evidence-based explanation. A Manager approves the selected
people before they become Project Team members. Team membership never assigns a
Task; a Task receives an assignee only through an explicit Manager action.

#### Scope

- Add Employee skills, skill evidence, capacity and leave.
- Add provenance-linked evidence from relevant completed work and review
  outcomes; never collapse it into one opaque Employee performance score.
- Add deterministic workload calculation.
- Add deterministic candidate hard filtering and weighted scoring.
- Add Project Team membership with an approval-backed recommendation lifecycle.
- Add versioned `recommend_project_team` and `analyze_workload` skills.
- Activate the Assignment Agent as an independent Specialist package with
  `agent.yaml`, typed contracts, Harness, assignment graph, skill/tool allowlist,
  deterministic ranking adapters, evaluators and tests.
- Register Project Team recommendation, workload and explicit Task assignment
  handoffs with the Orchestrator so typed evidence and action cards appear in
  the same conversation transcript.
- Use an LLM only to explain scores, evidence, risks and alternatives.
- Do not use a single opaque performance score. Keep historical outcomes as
  contextual, provenance-linked evidence rather than a global judgment about an
  Employee.
- Require Manager approval before applying an AI-recommended Project Team.
- Preserve direct authorized/audited manual Task assignment from the normal UI
  and from an explicit, unambiguous Manager chat command.
- Require a Task assignee to be an active same-tenant member of the approved
  Project Team. Capacity or workload warnings are shown but never cause the
  Agent to choose or replace an assignee automatically.
- Capture accept, override and reject feedback for recommendations.

#### Main files/modules

- Frontend `people`, `capacity`, `workload`, Project Team and
  recommendation-card features.
- Backend `people_capacity` module.
- Backend `planning/assignment` deterministic filtering, Project Team ranking,
  membership approval and explicit Task assignment application services.
- `ai/agents/assignment` for the Agent manifest, Harness, graph, prompts,
  evaluator and tests.
- `ai/skills/recommend_project_team` and `ai/skills/analyze_workload`, plus typed
  team and assignment tools that cannot alter deterministic eligibility,
  scores, approval or assignment authority.

#### Database and API

Database entities:

- `skills`
- `person_skills`
- `skill_evidence`
- `work_outcome_evidence`
- `capacity_entries`
- `leave_entries`
- `project_team_memberships`
- `recommendations`
- `candidate_scores`
- `recommendation_feedback`
- `skill_versions`

Required APIs:

- CRUD under `/api/v1/skills`
- CRUD under `/api/v1/members/{member_id}/skills`
- CRUD under `/api/v1/capacity`
- CRUD under `/api/v1/leave`
- `GET /api/v1/workload`
- `GET /api/v1/projects/{project_id}/team`
- `POST /api/v1/projects/{project_id}/team-recommendations`
- `POST /api/v1/tasks/{task_id}/assign`
- `GET /api/v1/recommendations/{recommendation_id}`
- `POST /api/v1/recommendations/{recommendation_id}/approve`
- `POST /api/v1/recommendations/{recommendation_id}/feedback`

#### Tests

- Capacity, leave and workload calculation tests.
- Candidate hard-constraint and score-reproducibility tests.
- Permission tests for profile evidence, Project Team membership and explicit
  Task assignments.
- Proof that an LLM response cannot alter candidate scores or eligibility.
- Proof that Project Team recommendation approval adds only Project Team
  memberships and assigns zero Tasks.
- Explicit Manager assignment without an additional approval request, including
  exact Task/person resolution, team-membership validation, authorization,
  idempotency and audit.
- Provider-failure fallback to an unexplained deterministic ranking.
- Recommendation acceptance, override and feedback tests.

#### Definition of Done

- The same inputs always produce the same ranking and score breakdown.
- An ineligible candidate cannot be approved even if named by the model.
- Manager can see evidence and workload behind each candidate.
- AI recommendation can add approved Project Team memberships but cannot assign
  any Task.
- A Task is assigned only when a Manager explicitly identifies the Task and
  Employee; the write proceeds after deterministic resolution, authorization,
  validation and audit.
- Project Team membership and Task assignment remain separate relations.
- Recommendation remains usable when explanation generation fails.
- The Orchestrator delegates permitted Project Team, workload and explicit
  assignment steps to the Assignment Agent without creating a second chat
  product or exposing Agent/workflow selection to the user.

#### Explicit non-goals

- Google Calendar availability.
- CP-SAT or portfolio-wide optimization.
- Learned ranking, effort estimation or performance scoring.
- Per-Task AI assignee recommendation.
- Automatic or bulk assignment from Project plan or Project Team approval.
- Autonomous reassignment and autonomous replanning.

#### Closure record — 2026-09-27

- `make lint`, `make typecheck` and `make test` passed: 745 backend/AI tests
  and 191 frontend tests in the default suite.
- `make migration-check` passed all 105 PostgreSQL integration tests at the
  single Alembic head `0016`, including tenant isolation, RLS, approval,
  idempotency, audit and tool evidence immutability checks.
- `make ai` passed 191 AI tests. `make eval` passed the 24 Phase 2 and 26
  bilingual Phase 3 mock cases, with zero approval bypasses, unauthorized
  delegations, peer handoffs and cross-tenant leaks.
- `make test-e2e` passed all six browser scenarios: People/Capacity,
  requirements and deterministic team recommendation, v1→v2 revision,
  stale-version denial, Team approval with zero Task assignments, explicit
  manual and exact-chat Task assignment, and the combined Project-plus-team
  Assistant checkpoint. A separate browser run with `APP_AI_PROVIDER=disabled`
  passed the manual Team recommendation, revision, approval and Task
  assignment flow.
- `make containers-config-check`, `make ai-worker-check` and
  `make containers-up` passed. PostgreSQL, backend API, frontend and the one
  AI worker were running; the first three health checks were healthy and the
  frontend returned HTTP 200.
- The Phase 3 Explicit non-goals audit found no new integration, deployment,
  solver, learned-ranking, automatic/bulk-assignment or Phase 4 runtime work.
  The existing demo `Kubernetes` skill label is catalog data, and protected
  attribute matches are verifier denial rules/tests. Transactional outbox
  records persist, while the existing local publisher stub logs unsupported
  dispatch attempts because no external consumer is configured.

This closes Phase 3 only. Optional Integrations, the Deployment Track and
Phase 4 remain inactive.

### Phase 4 — Manual and AI Daily Update, Blocker and Risk

#### User-visible outcome

An Employee can submit a daily update either through a structured manual form
or natural-language extraction. The Employee confirms AI extraction before
saving. A confirmed update records actual Task progress, remaining effort,
work completed, blockers and next steps, then deterministically refreshes the
actual progress of the related Project Week without rewriting its planned
baseline. A Manager can compare planned versus actual weekly progress, inspect
blockers and AI-assessed risk, including rationale and advisory recommendations or
weekly re-planning proposal.

#### Scope

- Add manual daily updates with Task-linked done, actual progress, remaining
  effort, blocker and next-step items.
- Add evidence, work logs, immutable confirmed progress observations and blocker
  lifecycle. A confirmed observation records what the Employee reported at that
  time; later updates append rather than overwrite history.
- Permit an Employee to report actual progress only for a currently assigned,
  visible Task. Tenant, assignment and Task-version checks are deterministic and
  are repeated when confirmation is applied.
- Deterministically derive current Task actual progress and Project Week actual
  metrics from confirmed observations and authoritative Task state. Keep the
  original Project Week plan, planned dates, planned effort and dependencies as
  a separate baseline.
- Compute planned-versus-actual deviation and workload inputs deterministically.
  Use the Model Gateway to assess risk from progress, remaining effort, deadlines,
  capacity, dependencies, activity and blocker age; no fixed additive factor points.
  Persist AI-authored score (0–100 or unavailable), rationale, cited observations,
  recommendations, context snapshot and model/prompt/schema/policy versions.
  Evidence support likewise uses the model score directly, retaining threshold 70
  and independent contradiction/coverage warnings. Legacy scores remain unchanged.
- Add AI extraction, exact Task-link suggestions, progress/remaining-effort
  extraction and blocker classification.
- Activate independent Daily Update and Risk Agent packages, each with a
  manifest, typed contracts, Harness, graph/skills/tools, evaluator and tests.
- Register Daily Update and blocker/risk handoffs with the Orchestrator.
  Employee messages produce a structured `Task / Done / Actual progress /
  Remaining effort / Blockers / Next steps` card that the Employee must correct
  or confirm before persistence.
- Surface confirmed blockers and their evidence to permitted Managers. The
  Assistant may notify or recommend a response. When a validated AI risk assessment suggests a
  schedule change, the Orchestrator may ask the already-active Planning Agent
  for an exact-version weekly re-planning proposal. The Risk Agent cannot edit
  the plan, Project Week, assignment or deadline autonomously.
- Require Employee confirmation before persisting extracted content.
- After confirmation, atomically persist the Daily Update, progress observation,
  work log, evidence and blocker changes, then refresh deterministic Task and
  Project Week actual metrics. This does not change the planned week, deadline,
  Milestone, dependency, assignee or planned baseline.
- Treat a reported `100%` as progress evidence, not automatic Task completion.
  Task completion continues through its deterministic status, acceptance and
  authorization rules.
- Let AI assess risk and propose actions for Manager review. Code applies LOW
  <30, MEDIUM 30–59 and HIGH >=60 to valid model scores. Model failure displays
  UNAVAILABLE with factual metrics/blockers; never fabricate a rule-derived score.
  Later explanations cannot overwrite a stored assessment; reassessment appends one.
- Add thresholded and deduplicated in-app risk notifications.
- Add deterministic Project reporting-window coverage and Manager-configured
  daily-summary automations. A Manager may create/edit/pause a schedule through
  chat or Settings; creation/change uses a confirmation card, while the scheduler
  and trigger idempotency remain deterministic.
- At 100% confirmed coverage when configured, or at the Manager-selected cutoff,
  create a permission-safe structured in-app digest. Missing reporters and
  snapshot time are explicit; severe blockers may alert immediately without
  waiting for the cutoff. Narrative management reporting remains Phase 5.
- Keep the whole flow usable through manual forms when AI is unavailable.

#### Main files/modules

- Frontend `daily_updates`, weekly planned-versus-actual progress, `blockers` and
  `risk` features.
- Backend `progress` module for confirmed observations, current Task actuals and
  deterministic Project Week aggregation.
- Backend `risk` module for deterministic context construction, typed AI
  assessment port/adapters, threshold policy, immutable snapshots and persistence.
- `ai/agents/daily_update` for extraction, linking, owner confirmation and
  persistence handoff.
- `ai/agents/risk` for permission-safe contextual assessment/explanations and
  advisory recommendations over versioned work facts and stored assessments.
- Backend automation/scheduler application service, PostgreSQL jobs and outbox;
  no Automation Agent or broker.
- `ai/evaluation/daily_update` for bilingual extraction, malformed output and
  provider-failure cases.

#### Database and API

Database entities:

- `daily_updates`
- `daily_update_items`
- `task_progress_observations`
- `project_week_progress_snapshots`
- `work_logs`
- `evidence`
- `blockers`
- `risk_assessments`
- `notifications`
- `automation_schedules`
- `automation_triggers`
- `daily_summary_snapshots`

Required APIs:

- `POST /api/v1/daily-updates`
- `GET /api/v1/daily-updates`
- `POST /api/v1/daily-updates/extract`
- `POST /api/v1/daily-updates/{draft_id}/confirm`
- `GET /api/v1/projects/{project_id}/weeks/{week_id}/progress`
- CRUD under `/api/v1/blockers`
- `GET /api/v1/risks`
- `POST /api/v1/risks/{risk_id}/explain`
- `GET /api/v1/notifications`
- CRUD/pause/resume under `/api/v1/automations/daily-summaries`

#### Tests

- Manual daily-update and blocker lifecycle tests.
- Bilingual extraction, exact Task-linking, actual-progress and
  remaining-effort evaluation cases.
- Employee correction and low-confidence fallback tests.
- Assigned-Task ownership, Task-version, authorization, RLS and negative
  cross-tenant tests for every confirmed progress observation.
- Atomic confirmation tests proving one retry creates one Daily Update, progress
  observation, work log and blocker mutation, and refreshes weekly actuals once.
- Planned-versus-actual aggregation tests covering multiple Tasks, missing
  reports, unknown remaining effort, reassignment after reporting and late or
  blocked work.
- Proof that confirmed actual progress changes no Project Week baseline, planned
  date, deadline, Milestone, dependency or assignee.
- Mock-provider risk score preservation, dependency context, schema/range/source
  validation, threshold boundaries and unavailable/retry tests. Test immutable
  snapshot replay; repeated live generation need not return the same score.
- Proof that AI explanation cannot mutate a risk score.
- Risk-triggered re-planning tests proving that the Risk Agent supplies only
  source-linked observations, the Planning Agent creates the weekly diff, completed weeks
  remain immutable, new Tasks remain unassigned and Manager approval is
  required before any plan change.
- Notification threshold and deduplication tests.
- Schedule timezone, coverage/cutoff, partial summary, severe-blocker immediate
  trigger, authorization and trigger idempotency tests.
- Provider timeout and malformed-output fallback tests.

#### Definition of Done

- Manual daily update, blocker, factual work metrics and Manager review remain
  available without a model provider; fresh AI risk is explicitly unavailable.
- AI-extracted data is not persisted until Employee confirmation.
- A confirmed update appends an immutable Task progress observation and updates
  deterministic current Task and Project Week actual metrics.
- Managers can compare the immutable weekly planned baseline with current actual
  progress, remaining effort, missing reports and blockers.
- Unknown or stale progress and remaining effort are shown explicitly and are
  never treated as zero.
- A reported completion or `100%` progress cannot bypass deterministic Task
  completion and Acceptance Criteria rules.
- Confirming a Daily Update never changes planned week, deadline, Milestone,
  dependency or assignee.
- Every risk score has an immutable input/result snapshot, citations and
  model/prompt/schema/policy provenance. Replay preserves the stored result;
  a fresh model assessment may differ and must create a new snapshot.
- Every AI explanation references stored assessment observations and permitted
  evidence. Contextual scores are AI assessments, not calibrated probabilities.
- A risk-triggered schedule response is an exact-version Planning proposal with
  a weekly before/after diff; it is never a direct Risk Agent mutation.
- AI failure never blocks manual submission or Manager risk review.
- Employee daily-update turns and Manager blocker review remain in the same
  conversation-first Assistant while respecting their different permissions.
- A configured daily summary runs once per reporting window, identifies missing
  updates, uses only confirmed evidence and reaches only currently permitted
  in-app recipients.

#### Explicit non-goals

- Trained risk or effort model.
- Automatic Task completion, assignment, planned-baseline mutation or
  re-planning from a Daily Update.
- External notifications.
- Management report narrative.
- Calendar and knowledge retrieval.

### Phase 5 — Management Report, Feedback and Evaluation Loop

#### User-visible outcome

A Manager can generate and review daily or weekly management reports. Numbers come from deterministic queries; AI can draft the narrative. Users can submit accept, edit or reject feedback, and the developer can run a versioned evaluation suite against accumulated permission-safe cases.

#### Scope

- Create deterministic daily and weekly report metric snapshots.
- Generate an optional LLM narrative from an immutable snapshot.
- Activate an independent Reporting Agent package with manifest, typed
  contracts, Harness, graph/skills/tools, numeric/evidence evaluators and tests.
- Register project-status and management-report handoffs with the Orchestrator;
  responses render verified metric/evidence cards inline and may link to the
  full report view.
- Upgrade Phase 4 daily-summary automations to optionally delegate narrative
  generation to the Reporting Agent while retaining the immutable snapshot and
  deterministic metrics-only fallback.
- Verify every narrative number against the snapshot.
- Require Manager review before publishing an AI-authored report.
- Allow direct publishing of a deterministic metrics-only report.
- Capture feedback with proposal/run/model/prompt/skill versions and later actual outcome.
- Redact and retain raw AI context for 30 days and redacted traces for 90 days.
- Add a small golden evaluation suite and an automated evaluation command.
- Add a transactional outbox worker for report generation, notification and retention jobs.

#### Main files/modules

- Frontend `reports`, `feedback` and minimal evaluation-status views.
- Backend `reporting` and `feedback` modules.
- Backend outbox worker, immutable metric snapshot and feedback persistence.
- `ai/agents/reporting` for verified narrative generation and scheduled/on-demand
  report handoffs.
- `ai/evaluation` for the versioned golden suite, evaluators and promotion gates.
- `ai/observability` for model/workflow/prompt/skill version and trace contracts.

#### Database and API

Database entities:

- `reports`
- `report_metric_snapshots`
- `feedback`
- `evaluation_cases`
- `evaluation_results`
- `outbox_events`
- Retention metadata on AI run/context records.

Required APIs:

- `POST /api/v1/reports`
- `POST /api/v1/reports/{report_id}/generate`
- `GET /api/v1/reports/{report_id}`
- `POST /api/v1/reports/{report_id}/publish`
- `POST /api/v1/feedback`
- `POST /api/v1/evaluations/runs`
- `GET /api/v1/evaluations/runs/{run_id}`

#### Tests

- Report aggregation and immutable-snapshot tests.
- Narrative numeric-consistency and unsupported-claim tests.
- Metrics-only fallback tests.
- AI report approval versus manual report publishing tests.
- Feedback provenance, redaction, deduplication and retention tests.
- Outbox retry and idempotent consumer tests.
- Core bilingual golden evaluation suite.
- End-to-end Core MVP flow from Manager project creation through feedback.

#### Definition of Done

- Every report number is traceable to an immutable metric snapshot.
- Invalid AI narrative is rejected or replaced by the metrics-only report.
- AI-authored reports require Manager approval; manual metrics reports do not.
- Feedback links input, output, versions, human correction and available outcome.
- Retention jobs delete expired raw context without deleting required business audit records.
- Phases 1–5 pass one automated end-to-end Core MVP scenario in Vietnamese and English.
- One conversation can route successive permitted planning, status, daily-update
  and reporting turns through typed Specialist handoffs without exposing
  internal agent/workflow names or granting
  cross-role capabilities.

#### Explicit non-goals

- Qdrant, hybrid retrieval and GraphRAG.
- Google Calendar or other external integrations.
- Kubernetes, Jenkins and GKE.
- Model training, self-hosting, distillation and learned optimization.

## 3. Core MVP Exit Gate

Optional integrations and the deployment track may start only when:

- All Phase 1–5 Definitions of Done pass.
- The manual product remains usable when all AI providers are disabled.
- All AI-originated mutations pass the documented human gate: owner confirmation
  for user-owned extracted drafts, Manager/Admin approval for organization-level
  proposals and policy approval for external/high-risk actions. Agents never
  approve their own output.
- Cross-tenant leakage and approval bypass tests report zero violations.
- All state-changing APIs are authorized, validated, idempotent where retryable, transactional and audited.
- Core workflow failure and fallback paths have automated tests.
- The Core MVP has been demoed end to end without unresolved severity-high defects.

## 4. Post-MVP Optional Integrations

These integrations are independent. Neither blocks the deployment track.

### I1 — Google Calendar

#### User-visible outcome

A Manager can include Google Calendar availability in explicit assignment
review and create an event after the assignment is confirmed.

#### Scope

- Google OAuth testing mode with a personal account during development.
- Availability lookup and event creation only.
- Google and mock adapters sharing one typed contract.
- Encrypted token storage, freshness indicators, retry and idempotency.
- External event creation always requires explicit approval.

#### Main files/modules

- Frontend integration settings and assignment availability UI.
- Backend `integrations/calendar` module.
- Google Calendar and mock adapters.

#### Database and API

Database entities:

- `calendar_connections`
- `availability_snapshots`
- `external_event_mappings`

APIs:

- `GET /api/v1/integrations/google-calendar/connect`
- `GET /api/v1/integrations/google-calendar/callback`
- `GET /api/v1/calendar/availability`
- `POST /api/v1/tasks/{task_id}/calendar-events`

#### Tests

- Adapter contract, OAuth expiry/revocation, token isolation, stale availability, duplicate event and provider-outage tests.

#### Definition of Done

- Calendar failure does not block assignment.
- Event creation is idempotent and occurs only after approval.
- Local and automated tests run without Google credentials.

#### Explicit non-goals

- Gmail, domain-wide delegation, two-way task sync and automatic rescheduling.

### I2 — Qdrant Hybrid Knowledge Retrieval

#### User-visible outcome

Users can find permitted organizational documents and receive an evidence-backed answer with citations.

#### Scope

- Document upload/versioning and object storage.
- Permission-aware indexing.
- Qdrant hybrid retrieval with tenant and permission metadata filters.
- Citation validation and evidence-only fallback.
- Direct PostgreSQL queries remain mandatory for transactional facts.

#### Main files/modules

- Frontend knowledge search and citation viewer.
- Backend `knowledge` module for canonical document metadata and permission
  enforcement.
- `ai/retrieval` for indexing, hybrid query, reranking and citation verification.
- Qdrant and object-storage adapters behind typed ports.

#### Database and API

Database entities:

- `documents`
- `document_versions`
- `document_chunks`
- `citations`
- `indexing_jobs`

APIs:

- `POST /api/v1/documents`
- `GET /api/v1/documents/{document_id}`
- `POST /api/v1/documents/{document_id}/reindex`
- `POST /api/v1/knowledge/search`
- `POST /api/v1/knowledge/answer`

#### Tests

- Retrieval recall, citation validity, tenant leakage, permission revocation, stale version, malicious document and Qdrant-outage tests.

#### Definition of Done

- Permission filtering happens before retrieval results enter model context.
- Every answer claim is cited or reported as insufficient evidence.
- Qdrant never becomes the source of truth for project/task facts.

#### Explicit non-goals

- GraphRAG, knowledge-graph extraction and automatic Work Graph mutation.

## 5. Post-MVP Deployment Track

This track may begin after the Core MVP Exit Gate. It does not wait for I1 or I2.

### D1 — Kubernetes Packaging with kind

#### User-visible outcome

No new product behavior; the stable Core MVP can be installed, upgraded and rolled back in local Kubernetes.

#### Scope

- OCI images for frontend, API and worker.
- Package `ai/` into the API and worker images; do not add an AI network service
  unless a later measured scaling or isolation requirement authorizes it.
- Helm chart with Namespace, Deployments, Services, Gateway/Ingress, ConfigMaps, Secrets, probes, resources, HPA, Jobs/CronJobs, Service Accounts and NetworkPolicies.
- kind integration environment and migration Job.

#### Main files/modules

- `deploy/helm/`
- `deploy/kind/`
- Application container build files.

#### Database and API

- No new business entities or APIs.
- Add `/api/v1/health/live`, `/api/v1/health/ready` and `/api/v1/health/startup`.

#### Tests

- Helm lint/template, kind install, upgrade, rollback, probe, migration Job and NetworkPolicy tests.

#### Definition of Done

- A documented command starts the application on kind.
- Upgrade and rollback preserve database and outbox correctness.
- Runtime behavior matches the Compose environment.

#### Explicit non-goals

- Jenkins, GKE, service mesh, custom operators, GitOps, multi-cluster and GPU workloads.

### D2 — Jenkins CI/CD

#### User-visible outcome

No new product feature; every release candidate is tested, evaluated, scanned and packaged reproducibly.

#### Scope

- Jenkins stages for lint, typecheck, tests, evaluation, dependency/image scan, image build, Helm checks and kind smoke tests.
- Immutable image tags and manual promotion approval.

#### Main files/modules

- `Jenkinsfile`
- Reusable CI scripts and artifact metadata.

#### Database and API

- No business database or API changes.

#### Tests

- Clean-build reproducibility, failure-path, secret-masking and artifact-provenance tests.

#### Definition of Done

- Failed tests, evaluation gates or security scans block promotion.
- Artifacts map to an exact commit and migration version.
- Jenkins and local development use the same quality commands.

#### Explicit non-goals

- Unapproved production deployment, GitOps and Jenkins-owned business migrations.

### D3 — GKE Autopilot

#### User-visible outcome

Pilot users can access the Core MVP through a stable HTTPS staging/production endpoint with monitoring, backup and rollback.

#### Scope

- GKE Autopilot in `asia-southeast1`.
- Managed/dedicated PostgreSQL, Redis, object storage and optional Qdrant outside Kubernetes.
- Workload Identity, Secret Manager, TLS, autoscaling, observability, backup/restore and rolling rollback.

#### Main files/modules

- `deploy/gke/`
- Production Helm values and operations runbooks.

#### Database and API

- No new business entities or APIs.
- Operational metrics and retention Jobs only.

#### Tests

- Staging load/soak, backup restore, pod interruption, migration compatibility, tenant isolation and rollback rehearsal.

#### Definition of Done

- Production uses immutable Jenkins artifacts.
- Backup restore and rollback have recorded evidence.
- No durable state exists only on pod filesystems.
- Agreed availability and latency SLOs are measured.

#### Explicit non-goals

- Service mesh, custom operators, multi-cluster, GPU pools and microservice decomposition.

## 6. Advanced Track

### A1 — Solver and Predictive ML

Activate only after stable historical outcomes, a versioned dataset, a deterministic baseline and a held-out evaluation set exist.

#### User-visible outcome

Managers can compare optimized assignment or replanning options with constraints, confidence and explanations.

#### Scope

- OR-Tools CP-SAT for constrained optimization.
- Tabular effort/risk models only when they outperform deterministic baselines.
- Shadow and canary evaluation before production use.

#### Main files/modules

- `ai/optimization`, `ai/ml` and `ai/evaluation` packages; deterministic hard
  constraints remain owned and verified by backend application/domain code.

#### Database and API

- `optimization_runs`, `simulations`, `feature_snapshots`, `dataset_versions`, `model_versions`.
- `/api/v1/simulations` and `/api/v1/optimization-runs`.

#### Tests

- Solver feasibility, calibration, drift, fairness slices, baseline comparison and rollback tests.

#### Definition of Done

- The new method beats its documented baseline and never violates hard constraints.
- Applying a replan remains a high-risk action requiring human approval.

#### Explicit non-goals

- LLM-based constraint solving, autonomous replanning and Employee performance judgment.

### A2 — Self-hosting and Distillation

Activate only after permission-safe reviewed training data, a bilingual held-out set, governance approval and a demonstrated cost/privacy/latency benefit exist.

#### User-visible outcome

Selected low-risk AI tasks can use a validated student model with hosted-model fallback.

#### Scope

- OpenAI-compatible self-hosted inference adapter.
- Teacher-data curation and SFT/LoRA experiments.
- Offline evaluation, shadow routing and canary promotion.
- Hosted OpenAI fallback when capability or verification fails.

#### Main files/modules

- `ai/model_gateway/adapters/self_hosted`.
- `ai/training`, `ai/evaluation` and versioned routing configuration.

#### Database and API

- `training_runs`, `model_artifacts`, `routing_decisions`.
- Admin-only `/api/v1/model-evaluations` and `/api/v1/model-routes`.

#### Tests

- Teacher/student quality, safety, structured output, load, timeout, fallback and rollback tests.

#### Definition of Done

- Student model passes the declared held-out quality and safety gates.
- Production routing is limited to approved low-risk workflows.
- Hosted fallback is verified under real failure scenarios.

#### Explicit non-goals

- Training on raw production data, immediate replacement of hosted models and MVP-cluster GPU deployment.

### A3 — GraphRAG

Activate only when a labeled multi-hop/global-query benchmark proves that direct query and Qdrant hybrid retrieval are insufficient.

#### User-visible outcome

Users can answer evidence-backed multi-hop or organization-wide relationship questions that the existing retrieval modes cannot answer reliably.

#### Scope

- Versioned knowledge entity/relation extraction.
- Multi-hop/global retrieval workflow.
- Permission propagation and graph-specific evaluation.
- Preserve the separation between inferred Knowledge Graph and verified Work Graph.

#### Main files/modules

- `ai/retrieval/graphrag` indexing/projection and retrieval adapter.
- `ai/evaluation/graphrag` dedicated benchmark and evaluator.

#### Database and API

- `knowledge_entities`, `knowledge_relations`, `graph_index_versions`.
- Extend `/api/v1/knowledge/search` with an explicit evaluated GraphRAG mode.

#### Tests

- Entity/relation quality, multi-hop benchmark, permission propagation, stale-source removal and cost/quality comparisons.

#### Definition of Done

- The benchmark shows a documented, material quality improvement over direct and hybrid retrieval.
- Every generated answer remains permission-filtered, source-versioned and cited.
- Inferred relations never become verified Work Graph facts automatically.
- Simple task, deadline, assignee and exact-document queries continue using direct or hybrid retrieval.
- GraphRAG can be disabled without affecting Core MVP or ordinary knowledge search.

#### Explicit non-goals

- GraphRAG as the default retrieval mode.
- Graph database as the transactional source of truth.
- Automatic mutation of Work Graph from model-inferred relations.

### A4 — Controlled Task Execution Agent

Activate only after Core MVP approval/tool boundaries are stable, each enabled
execution Skill has an artifact contract and golden suite, and an organization
policy explicitly permits the capability.

#### User-visible outcome

An authorized user can ask the Assistant to perform a bounded knowledge-work
task, inspect the produced artifact/evidence and accept, edit or reject it. The
Task Execution Agent never expands its own scope or approves its own output.

#### Scope

- Activate the Task Execution Agent package with manifest, typed objective and
  artifact contracts, Harness, execution graph, skill/tool allowlist, budgets,
  verifier, reviewer gate and evaluation suite.
- Require objective, allowed tools, expected artifact, success criteria,
  deadline/budget, reviewer and autonomy level for every execution run.
- Permit draft-only internal work first. External or irreversible side effects
  require exact approved action/version and transactional outbox execution.
- Keep all specialist handoffs hub-and-spoke through the Orchestrator.

#### Main files/modules

- `ai/agents/task_execution`.
- Versioned execution Skills and typed Tool adapters.
- Backend execution-task, artifact, approval and audit application services.

#### Database and API

- `execution_tasks`, `execution_artifacts`, `artifact_reviews` and associated
  Agent/Tool invocation records.
- APIs under `/api/v1/execution-tasks` for create, inspect, cancel and review.

#### Tests

- Tool sandbox/allowlist, budget/timeout, artifact schema, reviewer separation,
  approval binding, retry/idempotency, cross-tenant and unsafe-scope tests.

#### Definition of Done

- The Agent produces only the declared artifact using allowed Skills/Tools.
- A failed/cancelled run leaves no unapproved external side effect.
- Reviewer accept/edit/reject and every executed side effect are traceable to
  exact Agent, Skill, Tool, model, prompt and approval versions.

#### Explicit non-goals

- General computer-use autonomy, coding-agent behavior by default, self-created
  agents, peer-to-peer delegation and autonomous scope expansion.

## 7. Global Definition of Done

Every phase or track item is complete only when all applicable conditions pass:

1. The documented user-visible outcome can be demonstrated independently.
2. Lint, formatting checks, type checking, unit tests and integration tests pass.
3. Required end-to-end happy path and failure/fallback paths pass.
4. Database migrations, RLS, tenant indexes and rollback/forward-compatibility have been reviewed.
5. Authorization, audit and idempotency tests cover every new mutation.
6. AI behavior implemented under `ai/` has a valid versioned Agent manifest,
   typed contracts/handoffs, isolated Harness/skill/tool boundaries,
   deterministic verifier, recorded agent/manifest/model/prompt/workflow/skill/
   tool versions, evaluation cases and a non-AI fallback where the underlying
   product flow is essential.
7. Manual writes, AI-proposed writes, bulk changes, external side effects and high-risk actions follow their correct approval policy.
8. Public APIs remain under `/api/v1` and OpenAPI contracts are updated.
9. Local run and demo instructions are current.
10. No phase silently introduces work listed in its Explicit non-goals.
11. Activated multi-agent paths pass Orchestrator planning/delegation, bounded
    loop, inactive-agent, specialist-isolation, checkpoint/retry and safe trace
    tests; peer-to-peer delegation, approval bypass and cross-tenant leakage
    remain zero.
