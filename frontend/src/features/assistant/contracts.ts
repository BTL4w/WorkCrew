import { z } from "zod";

const uuid = z.uuid();
const strict = <T extends z.ZodRawShape>(shape: T) => z.object(shape).strict();

export const textBlockSchema = strict({ kind: z.literal("text"), text: z.string() });
export const activityBlockSchema = strict({
  kind: z.literal("activity"),
  label_key: z.string(),
  status: z.enum(["PENDING", "RUNNING", "COMPLETED", "FAILED"]),
  agent_id: z.string().nullable().optional(),
  workflow_run_id: uuid.nullable().optional(),
});
const publicEvidenceSchema = strict({
  evidence_id: z.string(),
  resource_type: z.string(),
  resource_id: uuid,
  version: z.number().int().positive().nullable().optional(),
});
export const workEvidenceBlockSchema = strict({
  kind: z.literal("work_evidence"),
  summary: z.string(),
  evidence: z.array(publicEvidenceSchema),
});
export const questionBlockSchema = strict({
  kind: z.literal("question"),
  question: z.string(),
  response_context: z.record(z.string(), z.unknown()),
});
export const capabilityUnavailableBlockSchema = strict({
  kind: z.literal("capability_unavailable"),
  capability: z.string(),
  message_key: z.string(),
});
export const planningRunBlockSchema = strict({
  kind: z.literal("planning_run"),
  workflow_run_id: uuid,
  status: z.string(),
});
export const proposalBlockSchema = strict({
  kind: z.literal("proposal"),
  workflow_run_id: uuid,
  proposal_id: uuid,
  proposal_version: z.number().int().positive(),
  approval_id: uuid.nullable().optional(),
  state: z.string().nullable().optional(),
  can_approve: z.boolean().nullable().optional(),
  read_only: z.boolean().default(false),
  current_version: z.number().int().positive().nullable().optional(),
  error_codes: z.array(z.string()).default([]),
  manual_fallback: z.string().nullable().optional(),
});
export const decisionResultBlockSchema = strict({
  kind: z.literal("decision_result"),
  workflow_run_id: uuid,
  decision: z.enum(["APPROVE", "REJECT", "UNKNOWN"]),
  proposal_id: uuid,
  proposal_version: z.number().int().positive(),
  project_id: uuid.nullable().optional(),
  continue_team: z.boolean().default(false),
});
export const teamRecommendationBlockSchema = strict({
  kind: z.literal("team_recommendation"),
  project_id: uuid,
  recommendation_id: uuid,
  recommendation_version: z.number().int().positive(),
  status: z.enum(["PROPOSED", "APPROVED", "REJECTED", "STALE"]),
  explanation_status: z.enum(["NOT_REQUESTED", "AVAILABLE", "UNAVAILABLE"]),
});
export const teamDecisionResultBlockSchema = strict({
  kind: z.literal("team_decision_result"),
  recommendation_id: uuid,
  recommendation_version: z.number().int().positive(),
  decision: z.enum(["APPROVE", "REJECT"]),
});
export const assignmentResultBlockSchema = strict({
  kind: z.literal("assignment_result"),
  task_id: uuid,
  task_version: z.number().int().positive(),
  membership_id: uuid,
  warning_codes: z.array(z.string()).default([]),
});
export const dailySummaryBlockSchema = strict({
  kind:z.literal("daily_summary"), project_id:uuid, draft_id:uuid.nullable(),
  expected_version:z.number().int().nonnegative(),operation:z.enum(["CONFIGURE","PAUSE","RESUME"]),
  needs_manager_confirmation:z.literal(true),
});
export const dailyUpdateBlockSchema = strict({
 kind:z.literal("daily_update"),draft_id:uuid,draft_version:z.number().int().positive(),
 task_id:uuid,task_version:z.number().int().positive(),assessment_id:uuid.nullable().default(null),
 needs_owner_confirmation:z.literal(true),
});
export const riskBlockSchema = strict({
  kind: z.literal("risk"), task_id: uuid, fingerprint: z.string(),
  content: strict({
    task_id: uuid, task_version: z.number().int().positive(),
    risk_assessment_id: uuid.nullable(), version: z.number().int().positive(), fingerprint: z.string(),
    state: z.enum(["READY", "PENDING", "STALE", "UNAVAILABLE"]),
    score: z.string().nullable(), band: z.enum(["LOW", "MEDIUM", "HIGH"]).nullable(),
    scope: z.enum(["MANAGER", "OWN_WORK"]), rationale: z.string(),
    permitted_sources: z.array(strict({id:z.string(),kind:z.string(),values:z.record(z.string(),z.unknown())})),
    observations:z.array(strict({id:z.string(),text:z.string(),source_ids:z.array(z.string())})),
    explanation: strict({
      observation_explanations:z.array(strict({text:z.string(),observation_ids:z.array(z.string()),source_ids:z.array(z.string()),assertions:z.array(strict({source_id:z.string(),field:z.string(),value:z.union([z.string(),z.number(),z.boolean(),z.null()])}))})),
      limitations:z.array(z.string()), recommendations:z.array(z.string()), replan_requested:z.boolean(),
    }),
    limitations:z.array(z.string()), recommendations:z.array(z.string()),
    affected_week_ids:z.array(uuid), fallback:z.boolean(),
  }),
});
const reportSummary = {
 project_id:uuid,project_label:z.string().max(200),snapshot_id:uuid,snapshot_hash:z.string().regex(/^[a-f0-9]{64}$/),
 period_start:z.string().max(10),period_end:z.string().max(10),timezone:z.string().max(100),report_kind:z.enum(["DAILY","WEEKLY"]),captured_at:z.iso.datetime(),
 metrics:z.array(strict({key:z.string().max(256),value:z.string().nullable(),unit:z.string().max(32),state:z.enum(["KNOWN","PARTIAL","UNKNOWN","STALE","NOT_APPLICABLE"]),time_basis:z.string().max(64)})).max(6),
 sources:z.array(strict({resource_type:z.string().max(100),resource_id:uuid,version:z.number().int().positive()})).max(8),limitations:z.array(z.string()).max(10),
};
export const reportBlockSchema = strict({kind:z.literal("report"),context_run_id:z.uuid(),...reportSummary,report_id:uuid,report_version_id:uuid,generation_state:z.string().max(32),href:z.string().regex(/^\/\?project=[0-9a-f-]{36}&report=[0-9a-f-]{36}&version=[0-9a-f-]{36}$/),needs_manager_review:z.literal(true)});
export const projectStatusBlockSchema = strict({kind:z.literal("project_status"),...reportSummary,context_run_id:uuid,analysis:z.array(z.string()).max(3),analysis_state:z.enum(["VERIFIED","UNAVAILABLE"])});

export const safeErrorBlockSchema = strict({
  kind: z.literal("safe_error"),
  code: z.string(),
  message_key: z.string(),
  manual_fallback: z.string().nullable().optional(),
});

export const assistantBlockSchema = z.discriminatedUnion("kind", [
  textBlockSchema,
  activityBlockSchema,
  workEvidenceBlockSchema,
  questionBlockSchema,
  capabilityUnavailableBlockSchema,
  planningRunBlockSchema,
  proposalBlockSchema,
  decisionResultBlockSchema,
  teamRecommendationBlockSchema,
  teamDecisionResultBlockSchema,
  assignmentResultBlockSchema,
  dailySummaryBlockSchema,
  dailyUpdateBlockSchema,
  riskBlockSchema,
  reportBlockSchema,
  projectStatusBlockSchema,
  safeErrorBlockSchema,
]);

export const conversationSchema = strict({
  id: uuid,
  locale: z.enum(["vi", "en"]),
  title: z.string().nullable(),
  status: z.string(),
  version: z.number().int().positive().default(1),
  is_pinned: z.boolean().default(false),
  last_message_sequence: z.number().int().nonnegative(),
  last_event_sequence: z.number().int().nonnegative(),
  created_at: z.iso.datetime(),
  updated_at: z.iso.datetime(),
});
export const assistantMessageSchema = strict({
  id: uuid,
  sequence: z.number().int().positive(),
  role: z.enum(["USER", "ASSISTANT", "SYSTEM"]),
  content_blocks: z.array(assistantBlockSchema),
  created_at: z.iso.datetime(),
});
export const conversationListSchema = strict({ items: z.array(conversationSchema) });
export const conversationSnapshotSchema = strict({
  conversation: conversationSchema,
  messages: z.array(assistantMessageSchema),
});
export const assistantTurnAcceptedSchema = strict({
  conversation_id: uuid,
  message_id: uuid,
  turn_id: uuid,
  orchestration_run_id: uuid,
  status: z.literal("QUEUED"),
});

const cardActionSchema = z.discriminatedUnion("kind", [
  strict({ kind: z.literal("PLANNING_INPUT"), workflow_run_id: uuid }),
  strict({ kind: z.literal("PLANNING_REVISE"), workflow_run_id: uuid, proposal_id: uuid }),
  strict({
    kind: z.literal("TEAM_REVISE"),
    recommendation_id: uuid,
    recommendation_version: z.number().int().positive(),
  }),
]);
export const postMessageInputSchema = strict({
  message: z.string().min(1).max(8000),
  locale: z.enum(["vi", "en"]),
  card_action: cardActionSchema.optional(),
});

export type AssistantBlock = z.infer<typeof assistantBlockSchema>;
export type AssistantConversation = z.infer<typeof conversationSchema>;
export type AssistantMessage = z.infer<typeof assistantMessageSchema>;
export type ConversationSnapshot = z.infer<typeof conversationSnapshotSchema>;
export type AssistantTurnAccepted = z.infer<typeof assistantTurnAcceptedSchema>;
export type PostMessageInput = z.infer<typeof postMessageInputSchema>;
