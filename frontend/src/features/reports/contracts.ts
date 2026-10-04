import { z } from "zod";

export const reportKindSchema = z.enum(["DAILY", "WEEKLY"]);
export const reportSchema = z.object({
  id: z.uuid(), organization_id: z.uuid(), project_id: z.uuid(), kind: reportKindSchema,
  locale: z.enum(["vi", "en"]), version: z.number().int().positive(), snapshot_id: z.uuid(),
  current_publication_id: z.uuid().nullable().default(null),
  selected_version_id: z.uuid(), created_by_membership_id: z.uuid(), narrative_requested: z.boolean(),
  created_at: z.string().datetime({ offset: true }),
});
const capturedSourceSchema = z.object({ resource_type: z.string(), resource_id: z.uuid(), version: z.number().int().positive(), fingerprint: z.string().nullable(), observed_at: z.string(), label: z.string().nullable().optional(), facts: z.record(z.string(), z.json()).optional() });
export const metricSchema = z.object({
  key: z.string(), value: z.string().nullable(), unit: z.enum(["COUNT", "HOURS", "FRACTION", "PERCENT", "SCORE", "DAYS"]),
  state: z.enum(["KNOWN", "PARTIAL", "UNKNOWN", "STALE", "NOT_APPLICABLE"]),
  time_basis: z.enum(["AT_CAPTURE", "IN_PERIOD", "DECLARED_REPORTING_DATE"]), policy_version: z.string(),
  source_refs: z.array(capturedSourceSchema),
  limitations: z.array(z.string()),
});
export const reportSourcesSchema = z.object({ snapshot_hash: z.string(), items: z.array(z.object({ source: capturedSourceSchema, freshness: z.enum(["CURRENT", "UPDATED", "UNAVAILABLE"]) })), receipts: z.array(z.object({ id: z.uuid(), project_id: z.uuid(), query_version: z.string(), catalog_version: z.string(), metric_keys: z.array(z.string()), row_count: z.number().int(), captured_at: z.string(), isolation: z.literal("repeatable read"), scope_hash: z.string() })), next_cursor: z.string().nullable(), total: z.number().int() });
const periodSchema = z.object({ kind: reportKindSchema, local_start: z.string(), local_end: z.string(), timezone: z.string(), start_utc: z.string(), end_utc: z.string(), observed_through: z.string(), partial_period: z.boolean() });
export const snapshotSchema = z.object({
  id: z.uuid(), organization_id: z.uuid(), project_id: z.uuid(), report_id: z.uuid(),
  captured_at: z.string(), snapshot_hash: z.string().regex(/^[a-f0-9]{64}$/), catalog_version: z.string(), query_version: z.string(),
  period: periodSchema, metrics: z.record(z.string(), metricSchema),
  sources: z.array(capturedSourceSchema),
  receipts: z.array(z.object({ id: z.uuid(), project_id: z.uuid(), query_version: z.string(), catalog_version: z.string(), metric_keys: z.array(z.string()), row_count: z.number().int(), captured_at: z.string(), isolation: z.literal("repeatable read"), scope_hash: z.string() })),
  limitations: z.array(z.string()),
});
export const publicationSchema = z.object({ id: z.uuid(), report_id: z.uuid(), report_version_id: z.uuid(), snapshot_hash: z.string().regex(/^[a-f0-9]{64}$/), publisher_membership_id: z.uuid(), decision_id: z.uuid(), published_at: z.string().datetime({ offset: true }) });
const narrativeSchema = z.object({ locale: z.enum(["vi","en"]), snapshot_id:z.uuid(), snapshot_hash:z.string(), blocks:z.array(z.discriminatedUnion("kind",[
  z.object({id:z.string(),section:z.string(),kind:z.literal("FACT"),template:z.string(),bindings:z.array(z.object({metric_key:z.string()})),source_bindings:z.array(capturedSourceSchema.omit({observed_at:true,label:true,facts:true})).optional()}),
  z.object({id:z.string(),section:z.string(),kind:z.enum(["INTERPRETATION","RECOMMENDATION","LIMITATION"]),text:z.string(),source_refs:z.array(capturedSourceSchema.omit({observed_at:true,label:true,facts:true})),assumptions:z.array(z.string())})])) });
export const reportResultSchema = z.object({
  report: reportSchema, snapshot: snapshotSchema,
  selected_version: z.object({ id: z.uuid(), report_id: z.uuid(), snapshot_id: z.uuid(), origin: z.enum(["METRICS_ONLY", "AI_PROPOSED"]), locale: z.enum(["vi", "en"]), created_at: z.string(), narrative: narrativeSchema.nullable().optional(), rendered_facts: z.record(z.string(),z.string()).optional(), provenance: z.record(z.string(),z.json()).optional() }),
  publications: z.array(publicationSchema), generation_state: z.enum(["NOT_REQUESTED", "QUEUED", "RUNNING", "AWAITING_REVIEW", "AI_UNAVAILABLE", "FAILED"]), generation_id: z.uuid().nullable().optional(), metrics_version_id: z.uuid().nullable().optional(), replayed: z.boolean(),
});
export const reportPageSchema = z.object({ items: z.array(reportSchema), page: z.number().int(), page_size: z.number().int(), total: z.number().int() });
export type Report = z.infer<typeof reportSchema>;
export type ReportResult = z.infer<typeof reportResultSchema>;
export type Metric = z.infer<typeof metricSchema>;
export type ReportInput = { project_id: string; kind: "DAILY" | "WEEKLY"; period_start?: string; timezone?: string; locale: "vi" | "en"; narrative_enabled: boolean };

export const reportDefaultsSchema = z.object({ timezone: z.string(), period_start: z.string() });

export type PublishReportInput = { mode: "METRICS_ONLY"; report_version_id: string; snapshot_hash: string };
