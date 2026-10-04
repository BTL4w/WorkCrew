import { z } from "zod";

export const workflowRunStatusSchema = z.enum([
  "QUEUED",
  "RUNNING",
  "NEEDS_INPUT",
  "WAITING_FOR_DECISION",
  "COMPLETED",
  "FAILED",
]);
export const proposalStatusSchema = z.enum([
  "DRAFT",
  "VALIDATING",
  "READY_FOR_DECISION",
  "APPROVED",
  "REJECTED",
  "STALE",
]);

const nullableDate = z.iso.date().nullable();
const projectDraftSchema = z.object({
  title: z.string().trim().min(1).max(200),
  description: z.string().max(5000).nullable(),
  start_date: nullableDate,
  due_date: nullableDate,
});
const goalDraftSchema = z.object({
  title: z.string().trim().min(1).max(200),
  description: z.string().max(5000).nullable(),
  expected_outcomes: z.array(z.string().trim().min(1).max(1000)),
  target_date: nullableDate,
});
const milestoneDraftSchema = z.object({
  ref: z.string().trim().min(1).max(100),
  title: z.string().trim().min(1).max(200),
  description: z.string().max(5000).nullable(),
  due_date: nullableDate,
});
const projectWeekDraftSchema = z.object({
  ref: z.string().trim().min(1).max(100),
  week_number: z.number().int().positive(),
  start_date: z.iso.date(),
  end_date: z.iso.date(),
  objective: z.string().trim().min(1).max(2000),
});
const taskDraftSchema = z.object({
  ref: z.string().trim().min(1).max(100),
  project_week_ref: z.string().trim().max(100).default(""),
  milestone_ref: z.string().trim().min(1).max(100).nullable(),
  title: z.string().trim().min(1).max(200),
  description: z.string().max(5000).nullable(),
  due_date: nullableDate,
  assignee_membership_id: z.uuid().nullable(),
  required_skill_labels: z.array(z.string().trim().min(1).max(80)).max(20).default([]),
  estimated_effort_hours: z.number().int().positive().max(10000).default(1),
  acceptance_criteria: z.array(z.string().trim().min(1).max(1000)),
});
const dependencyDraftSchema = z.object({
  predecessor_ref: z.string().trim().min(1).max(100),
  successor_ref: z.string().trim().min(1).max(100),
});
const assumptionSchema = z.object({
  description: z.string(),
  source: z.string(),
});

const riskReplanMetadataSchema = z.object({
  binding: z.object({task_id:z.string(),risk_assessment_id:z.string(),fingerprint:z.string(),observation_ids:z.array(z.string()),affected_week_ids:z.array(z.string())}),
  before:z.object({project_id:z.string(),project_version:z.number(),project_title:z.string(),project_description:z.string().nullable(),
    weeks:z.array(z.object({id:z.string(),version:z.number(),week_number:z.number(),start_date:z.string(),end_date:z.string(),objective:z.string(),status:z.string(),baseline_id:z.string().nullable(),baseline_sequence:z.number().nullable()})),
    tasks:z.array(z.object({id:z.string(),version:z.number(),project_week_id:z.string().nullable(),title:z.string(),description:z.string().nullable(),due_date:z.string().nullable(),estimated_effort_hours:z.number().nullable(),assignee_membership_id:z.string().nullable(),status:z.string(),required_skill_labels:z.array(z.string()),acceptance_criteria:z.array(z.string())}).passthrough()),
  }),
});

export const proposalContentSchema = z.object({
  risk_replan:riskReplanMetadataSchema.optional(),
  project: projectDraftSchema,
  goal: goalDraftSchema,
  milestones: z.array(milestoneDraftSchema),
  project_weeks: z.array(projectWeekDraftSchema).default([]),
  tasks: z.array(taskDraftSchema),
  dependencies: z.array(dependencyDraftSchema),
  assumptions: z.array(assumptionSchema),
});

export const validationItemSchema = z.object({
  path: z.string().optional().default(""),
  code: z.string(),
  message_key: z.string().optional(),
}).passthrough();
export const validationResultSchema = z.object({
  can_approve: z.boolean().optional().default(false),
  errors: z.array(validationItemSchema).default([]),
  warnings: z.array(validationItemSchema).default([]),
}).passthrough();

const previousProposalVersionSchema = z.object({
  version: z.number().int().positive(),
  content: proposalContentSchema,
  field_provenance: z.record(z.string(), z.unknown()),
  creator_type: z.enum(["AI_SYSTEM", "HUMAN_MANAGER", "UNKNOWN"]),
});
export const proposalSnapshotSchema = z.object({
  proposal_id: z.uuid(),
  approval_id: z.uuid().nullable(),
  status: proposalStatusSchema,
  version: z.number().int().positive(),
  validation_result: validationResultSchema,
  content: proposalContentSchema,
  change_summary: z.string().nullable(),
  field_provenance: z.record(z.string(), z.unknown()),
  creator_type: z.enum(["AI_SYSTEM", "HUMAN_MANAGER", "UNKNOWN"]),
  previous_version: previousProposalVersionSchema.nullable(),
});
export const timelineItemSchema = z.object({
  sequence: z.number().int().positive(),
  event_type: z.string(),
  payload: z.record(z.string(), z.unknown()),
  occurred_at: z.iso.datetime(),
});
export const workflowRunSchema = z.object({
  id: z.uuid(),
  project_id: z.uuid().nullable(),
  status: workflowRunStatusSchema,
  workflow_name: z.string(),
  workflow_version: z.string(),
  verifier_version: z.string(),
  input_goal_text: z.string(),
  version: z.number().int().positive(),
  created_at: z.iso.datetime(),
  updated_at: z.iso.datetime(),
  current_stage: z.string().nullable().default(null),
  current_proposal: proposalSnapshotSchema.nullable().default(null),
  public_timeline: z.array(timelineItemSchema).default([]),
  allowed_actions: z.array(z.enum(["MESSAGE", "EDIT_PROPOSAL", "DECIDE_APPROVAL"])).default([]),
});
export const workflowRunReferenceSchema = z.object({
  run_id: z.uuid(),
  status: workflowRunStatusSchema,
  version: z.number().int().positive(),
});
export const workflowRunListSchema = z.object({ items: z.array(workflowRunSchema) });
export const proposalReferenceSchema = z.object({
  proposal_id: z.uuid(),
  workflow_run_id: z.uuid(),
  status: proposalStatusSchema,
  version: z.number().int().positive(),
  content: proposalContentSchema,
});
export const proposalVersionSchema = z.object({
  proposal_id: z.uuid(),
  workflow_run_id: z.uuid(),
  version: z.number().int().positive(),
  current_version: z.number().int().positive(),
  content: proposalContentSchema,
  creator_type: z.enum(["AI_SYSTEM", "HUMAN_MANAGER", "UNKNOWN"]),
});
const createdBusinessIdsSchema = z.object({
  project_id: z.uuid().nullable(),
  goal_id: z.uuid().nullable(),
  milestone_ids: z.array(z.uuid()),
  task_ids: z.array(z.uuid()),
  dependency_ids: z.array(z.uuid()),
  acceptance_criterion_ids: z.array(z.uuid()),
});
export const approvalResultSchema = z.object({
  approval: z.object({ id: z.uuid(), status: z.enum(["APPROVED", "REJECTED"]) }),
  proposal: z.object({
    id: z.uuid(),
    version: z.number().int().positive(),
    status: z.enum(["APPROVED", "REJECTED"]),
  }),
  created: createdBusinessIdsSchema,
  workflow_run_id: z.uuid(),
  finalization_job_id: z.uuid(),
});

export type ProposalContent = z.infer<typeof proposalContentSchema>;
export type WorkflowRun = z.infer<typeof workflowRunSchema>;
export type WorkflowRunReference = z.infer<typeof workflowRunReferenceSchema>;
export type ApprovalResult = z.infer<typeof approvalResultSchema>;
