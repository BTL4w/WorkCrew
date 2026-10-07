import { z } from "zod";
export const evaluationRequestSchema = z.object({dataset_version_id:z.uuid(),provider:z.enum(["mock","hosted"]).default("mock")}).strict();
const measurement = z.object({id:z.string(),passed:z.boolean(),skipped:z.boolean(),bindings_checked:z.number().int().nonnegative(),bindings_correct:z.number().int().nonnegative(),refs_checked:z.number().int().nonnegative(),refs_valid:z.number().int().nonnegative(),input_tokens:z.number().nullable(),output_tokens:z.number().nullable(),latency_ms:z.number().nonnegative()}).passthrough();
export const evaluationRunSchema = z.object({
  id:z.uuid(),dataset_version_id:z.uuid(),dataset_version:z.number().int().positive(),dataset_hash:z.string().regex(/^[a-f0-9]{64}$/),dataset_policy_version:z.string(),provider:z.enum(["mock","hosted"]),provider_policy_version:z.literal("report-eval-provider.v1"),
  status:z.enum(["QUEUED","RUNNING","PASSED","FAILED","CANCELLED"]),failure_kind:z.enum(["GATE","WORKER","AUTHORIZATION","POLICY"]).nullable(),safe_error_code:z.string().nullable(),
  result:z.object({total:z.number().int().nonnegative(),passed:z.number().int().nonnegative(),failed:z.number().int().nonnegative(),skipped:z.number().int().nonnegative(),gate_passed:z.boolean(),gate:z.object({missing_coverage:z.array(z.string())}).passthrough(),hosted_quality:z.string(),limitations:z.array(z.string()),cases:z.array(measurement).optional()}).nullable(),
}).passthrough();
export type EvaluationRequest=z.input<typeof evaluationRequestSchema>;
export type EvaluationRun=z.infer<typeof evaluationRunSchema>;
