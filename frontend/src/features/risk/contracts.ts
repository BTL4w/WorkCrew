import { z } from "zod";
const score = z.union([z.string().regex(/^(?:\d+(?:\.\d+)?)$/), z.number().finite()]).refine(value => Number(value) >= 0 && Number(value) <= 100);
const fact = z.object({ id: z.string(), kind: z.enum(["TASK", "PROGRESS", "BLOCKER", "DEPENDENCY", "CAPACITY", "WARNING", "BASELINE"]), values: z.record(z.string(), z.unknown()) });
export const riskSchema = z.object({
 id: z.uuid(), task_id: z.uuid(), task_version: z.number().int(), state: z.enum(["PENDING", "READY", "UNAVAILABLE", "STALE"]), evaluated_at: z.string(),
 input_snapshot: z.object({ task_id: z.uuid(), task_version: z.number().int(), facts: z.array(fact), missing: z.array(z.string()) }).nullable(),
 judgment: z.object({ score: score.nullable(), rationale: z.string(), observations: z.array(z.object({ text: z.string(), source_ids: z.array(z.string()) })), limitations: z.array(z.string()), recommendations: z.array(z.string()) }).nullable(),
 band: z.enum(["LOW", "MEDIUM", "HIGH"]).nullable(), limitation: z.string(), model_ref: z.string().nullable(), policy_version: z.literal("risk.ai.v1"), prompt_version: z.literal("risk-assessment.v1"), schema_version: z.literal("risk-judgment.v1"),
});
export const reviewSchema = z.object({ id: z.uuid(), risk_id: z.uuid(), actor_membership_id: z.uuid(), at: z.string(), disposition: z.enum(["NEEDS_FOLLOWUP", "ACCEPTED_EXPLANATION", "RESOLVED"]), reason: z.string() });
export const notificationSchema = z.object({ id: z.uuid(), task_id: z.uuid(), risk_id: z.uuid(), kind: z.enum(["HIGH_RISK", "SEVERE_BLOCKER", "EVIDENCE_REVIEW"]), created_at: z.string(), read: z.boolean() });
export type Risk = z.infer<typeof riskSchema>;
export type Review = z.infer<typeof reviewSchema>;
export const weeklyRiskSchema = z.object({ project_week_id: z.uuid(), state: z.enum(["READY", "PARTIAL", "UNAVAILABLE"]), score: score.nullable(), band: z.enum(["LOW", "MEDIUM", "HIGH"]).nullable(), task_count: z.number().int(), assessed_count: z.number().int(), unavailable_task_ids: z.array(z.uuid()) });
export const confirmedWarningsSchema = z.object({ assessment_id: z.string(), update_id: z.string().optional(), acknowledged_at: z.string(), warnings: z.array(z.object({ id: z.string(), code: z.string() })) });
