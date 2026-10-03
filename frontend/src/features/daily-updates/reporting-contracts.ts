import { z } from "zod";
import { blockerCommandSchema } from "@/features/blockers/contracts";
const decimal=z.string().regex(/^-?\d+(\.\d+)?$/);
export const selectedEvidenceSchema=z.object({evidence_id:z.string().uuid(),version:z.number().int().positive()});
export const reportingItemSchema=z.object({
 task_id:z.string().uuid(),expected_task_version:z.number().int().positive(),expected_progress_version:z.number().int().nonnegative(),
 reported_percent:decimal,remaining_hours:decimal.nullable().optional(),spent_hours:decimal.nullable().optional(),reporting_date:z.string(),done_text:z.string(),next_steps:z.string().optional(),
 blocker_commands:z.array(blockerCommandSchema).optional(),evidence_refs:z.array(selectedEvidenceSchema).optional(),corrects_observation_id:z.string().uuid().nullable().optional(),correction_reason:z.string().optional(),
});
export const reportingContextSchema=z.object({task_id:z.string().uuid(),task_version:z.number().int(),progress_version:z.number().int(),reported_percent:decimal.nullable(),remaining_hours:decimal.nullable(),reporting_timezone:z.string(),reporting_date:z.string(),project_week_state:z.enum(["LINKED","NO_PROJECT_WEEK"]),evidence_refs:z.array(selectedEvidenceSchema)});
export const dailyDraftSchema=z.object({id:z.string().uuid(),version:z.number().int(),content_hash:z.string(),items:z.array(reportingItemSchema),assessment_state:z.enum(["UNAVAILABLE","PENDING","READY","NOT_ASSESSED_NO_EVIDENCE"]),reporting_timezone:z.string(),confirmed_update_id:z.string().uuid().nullable()});
export const observationSchema=z.object({id:z.string().uuid(),update_id:z.string().uuid(),item:reportingItemSchema,progress_version:z.number().int(),reporting_at:z.string(),confirmed_at:z.string(),reporting_timezone:z.string(),late:z.boolean(),project_week_state:z.enum(["LINKED","NO_PROJECT_WEEK"])});
export const confirmedUpdateSchema=z.object({id:z.string().uuid(),draft_id:z.string().uuid(),observations:z.array(observationSchema),assessment_state:z.enum(["UNAVAILABLE","PENDING","READY","NOT_ASSESSED_NO_EVIDENCE"])});
export type ReportingItem=z.infer<typeof reportingItemSchema>;
export type DailyDraft=z.infer<typeof dailyDraftSchema>;
export type Observation=z.infer<typeof observationSchema>;
export type SelectedEvidence=z.infer<typeof selectedEvidenceSchema>;
export const draftAssessmentSchema=z.object({
 id:z.string().uuid().nullable(),draft_id:z.string().uuid(),draft_version:z.number().int(),state:z.enum(["PENDING","READY","UNAVAILABLE","NOT_ASSESSED_NO_EVIDENCE","STALE"]),
 result:z.object({score:decimal.nullable(),warning_codes:z.array(z.string()),assessed_count:z.number().int(),total_count:z.number().int(),rule_version:z.literal("evidence-support.v1")}).nullable(),
 claims:z.array(z.object({id:z.string(),source_span:z.string(),category:z.enum(["WORK","OUTPUT","ACCEPTANCE_CRITERION"]),checkability:z.boolean(),text:z.string(),task_id:z.string().uuid().nullable(),evidence_refs:z.array(selectedEvidenceSchema)})),
 findings:z.array(z.object({claim_id:z.string(),finding:z.enum(["SUPPORTED","PARTIAL","UNSUPPORTED","CONTRADICTED","UNASSESSABLE"]),source_refs:z.array(selectedEvidenceSchema),limitation:z.string()})),
 coverage:z.object({processed_count:z.number().int(),total_count:z.number().int()}),warnings:z.array(z.object({id:z.string().uuid(),code:z.enum(["LOW_SUPPORT","CONTRADICTION","INSUFFICIENT_ASSESSMENT"])})),limitation:z.string(),
});
export type DraftAssessment=z.infer<typeof draftAssessmentSchema>;
