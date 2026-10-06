import {z} from "zod";
export const feedbackSchema=z.object({id:z.uuid(),report_id:z.uuid(),report_version_id:z.uuid(),original_version_id:z.uuid(),generation_id:z.uuid(),actor_membership_id:z.uuid(),decision_id:z.uuid().nullable(),kind:z.enum(["TERMINAL_QUALITY","ADVISORY"]),decision:z.enum(["ACCEPT","EDIT","REJECT"]),reason:z.string().nullable(),provenance:z.record(z.string(),z.json()),created_at:z.string()});
export const feedbackResultSchema=z.object({feedback:feedbackSchema,replayed:z.boolean()});
export type FeedbackInput={report_id:string;report_version_id:string;decision:"ACCEPT"|"EDIT"|"REJECT";reason:string};
