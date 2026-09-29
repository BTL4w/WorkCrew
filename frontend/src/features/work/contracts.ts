import { z } from "zod";

import { membershipRoleSchema } from "@/shared/api/contracts";

export const taskStatusSchema = z.enum(["TO_DO", "IN_PROGRESS", "DONE"]);

export const projectCreateSchema = z.object({
  name: z.string().min(1).max(160),
  description: z.string().max(5000).nullable().optional(),
});

export const projectUpdateSchema = z.object({
  name: z.string().min(1).max(160).nullable().optional(),
  description: z.string().max(5000).nullable().optional(),
});

export const taskCreateSchema = z.object({
  project_id: z.uuid(),
  project_week_id: z.uuid(),
  milestone_id: z.uuid().nullable().optional(),
  title: z.string().min(1).max(200),
  description: z.string().max(10000).nullable().optional(),
  assignee_membership_id: z.uuid().nullable().optional(),
  required_skill_labels: z.array(z.string()).max(20).default([]),
  estimated_effort_hours: z.number().int().positive(),
  due_date: z.iso.date().nullable().optional(),
});

export const taskUpdateSchema = z.object({
  title: z.string().min(1).max(200).nullable().optional(),
  description: z.string().max(10000).nullable().optional(),
  assignee_membership_id: z.uuid().nullable().optional(),
  due_date: z.iso.date().nullable().optional(),
  milestone_id: z.uuid().nullable().optional(),
  project_week_id: z.uuid().nullable().optional(),
  required_skill_labels: z.array(z.string()).max(20).nullable().optional(),
  estimated_effort_hours: z.number().int().positive().nullable().optional(),
});
export const completionEvidenceRefSchema = z.object({
  evidence_id: z.uuid(), version: z.number().int().positive(),
});
export const criterionAttestationRequestSchema = z.object({
  criterion_id: z.uuid(), version: z.number().int().positive(), confirmed: z.boolean(),
  evidence_refs: z.array(completionEvidenceRefSchema).default([]),
});
export const taskStatusRequestSchema = z.object({
  to_status: taskStatusSchema,
  attestations: z.array(criterionAttestationRequestSchema).default([]),
});

export const explicitAssignmentRequestSchema = z.object({
  assignee_membership_id: z.uuid(),
  expected_task_version: z.number().int().positive(),
}).strict();

export const projectSchema = z.object({
  id: z.uuid(),
  name: z.string(),
  description: z.string().nullable(),
  version: z.number().int(),
  created_at: z.iso.datetime(),
  updated_at: z.iso.datetime(),
});

export const projectPageSchema = z.object({
  items: z.array(projectSchema),
  page: z.number().int(),
  page_size: z.number().int(),
  total: z.number().int(),
});

export const memberSchema = z.object({
  membership_id: z.uuid(),
  display_name: z.string(),
  role: membershipRoleSchema,
  is_active: z.boolean(),
});

export const memberPageSchema = z.object({
  items: z.array(memberSchema),
  page: z.number().int(),
  page_size: z.number().int(),
  total: z.number().int(),
});

export const assigneeSchema = z.object({
  membership_id: z.uuid(),
  display_name: z.string(),
});

export const taskSchema = z.object({
  id: z.uuid(),
  project_id: z.uuid(),
  project_week_id: z.uuid().nullable(),
  milestone_id: z.uuid().nullable(),
  title: z.string(),
  description: z.string().nullable(),
  assignee: assigneeSchema.nullable(),
  required_skill_labels: z.array(z.string()),
  estimated_effort_hours: z.number().int().positive().nullable(),
  status: taskStatusSchema,
  due_date: z.iso.date().nullable(),
  version: z.number().int(),
  created_at: z.iso.datetime(),
  updated_at: z.iso.datetime(),
});

export const taskPageSchema = z.object({
  items: z.array(taskSchema),
  page: z.number().int(),
  page_size: z.number().int(),
  total: z.number().int(),
});

export const assignmentWarningSchema = z.object({ code: z.string() }).strict();

export const explicitAssignmentResponseSchema = z.object({
  task: taskSchema,
  warnings: z.array(assignmentWarningSchema),
  effective_capacity_hours: z.number().int().nonnegative(),
  workload_before_hours: z.number().int().nonnegative(),
  workload_after_hours: z.number().int().nonnegative(),
}).strict();

export type Project = z.infer<typeof projectSchema>;
export type Member = z.infer<typeof memberSchema>;
export type Task = z.infer<typeof taskSchema>;
export type TaskStatus = z.infer<typeof taskStatusSchema>;
export type ProjectPage = z.infer<typeof projectPageSchema>;
export type MemberPage = z.infer<typeof memberPageSchema>;
export type TaskPage = z.infer<typeof taskPageSchema>;
export type ExplicitAssignmentResponse = z.infer<typeof explicitAssignmentResponseSchema>;
