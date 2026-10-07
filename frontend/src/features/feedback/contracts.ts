import {z} from "zod";
export const feedbackSchema=z.object({id:z.uuid(),report_id:z.uuid(),report_version_id:z.uuid(),original_version_id:z.uuid(),generation_id:z.uuid(),actor_membership_id:z.uuid(),decision_id:z.uuid().nullable(),kind:z.enum(["TERMINAL_QUALITY","ADVISORY"]),decision:z.enum(["ACCEPT","EDIT","REJECT"]),reason:z.string().nullable(),provenance:z.record(z.string(),z.json()),created_at:z.string()});
export const feedbackResultSchema=z.object({feedback:feedbackSchema,replayed:z.boolean()});
export type FeedbackInput={report_id:string;report_version_id:string;decision:"ACCEPT"|"EDIT"|"REJECT";reason:string};
export const outcomeSchema=z.object({id:z.uuid(),feedback_id:z.uuid(),actor_membership_id:z.uuid(),schema_version:z.literal("feedback-outcome.v1"),source:z.object({source_type:z.enum(["TASK_TRANSITION","TASK_ACTUALS","BLOCKER_RESOLUTION"]),source_id:z.uuid(),source_version:z.number().int().nonnegative()}),state:z.enum(["AVAILABLE","UNKNOWN"]),facts:z.record(z.string(),z.json()),occurred_at:z.string().nullable(),recorded_at:z.string()});
export const reviewRatesSchema=z.object({reviewed_generation_count:z.number().int().nonnegative(),accept_count:z.number().int().nonnegative(),edit_count:z.number().int().nonnegative(),reject_count:z.number().int().nonnegative(),accept_percent:z.string().nullable(),edit_percent:z.string().nullable(),reject_percent:z.string().nullable(),pending_generation_count:z.number().int().nonnegative(),failed_generation_count:z.number().int().nonnegative(),manual_report_count:z.number().int().nonnegative()});
export type Feedback=z.infer<typeof feedbackSchema>;
export type FeedbackOutcome=z.infer<typeof outcomeSchema>;
export type ReviewRates=z.infer<typeof reviewRatesSchema>;
