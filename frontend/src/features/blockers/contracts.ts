import { z } from "zod";

export const severitySchema = z.enum(["LOW", "MEDIUM", "HIGH", "CRITICAL"]);
export const blockerActionSchema = z.enum(["CREATE", "EDIT", "ACKNOWLEDGE", "RESOLVE", "REOPEN", "ARCHIVE"]);
const evidenceSchema = z.object({ evidence_id: z.string().uuid(), version: z.number().int().positive() });
export const blockerCommandSchema = z.object({
  task_id: z.string().uuid(), expected_task_version: z.number().int().positive(),
  action: blockerActionSchema, severity: severitySchema.optional(), text: z.string().max(4000).optional(),
  evidence_refs: z.array(evidenceSchema).optional(), blocker_id: z.string().uuid().nullable().optional(),
  expected_blocker_version: z.number().int().positive().nullable().optional(),
});
export const blockerSchema = z.object({
  id: z.string().uuid(), task_id: z.string().uuid(), created_by_membership_id: z.string().uuid(),
  version: z.number().int().positive(), severity: severitySchema, text: z.string(),
  status: z.enum(["OPEN", "ACKNOWLEDGED", "RESOLVED"]), archived: z.boolean(),
  evidence_refs: z.array(evidenceSchema), created_at: z.string(), updated_at: z.string(),
});
export const transitionSchema = z.object({
  id: z.string().uuid(), blocker_id: z.string().uuid(), version: z.number().int().positive(),
  actor_membership_id: z.string().uuid(), action: blockerActionSchema,
  from_status: z.enum(["OPEN", "ACKNOWLEDGED", "RESOLVED"]).nullable(),
  to_status: z.enum(["OPEN", "ACKNOWLEDGED", "RESOLVED"]), at: z.string(), snapshot: blockerSchema,
});
export type Blocker = z.infer<typeof blockerSchema>;
export type BlockerCommand = z.infer<typeof blockerCommandSchema>;
