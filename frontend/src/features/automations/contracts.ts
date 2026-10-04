import { z } from "zod";
export const commandSchema = z.object({
  project_id: z.uuid(),
  timezone: z.string(),
  weekdays: z.array(z.number().int()),
  cutoff: z.string(),
  send_when_complete: z.boolean(),
  partial_at_cutoff: z.boolean(),
  recipients: z.array(z.uuid()),
});
export const scheduleSchema = commandSchema.extend({
  id: z.uuid(),
  version: z.number().int(),
  paused: z.boolean(),
  effective_at: z.string().nullable(),
});
export const draftSchema = z.object({
  id: z.uuid(),
  command: commandSchema,
  expected_version: z.number().int(),
  expires_at: z.string(),
  effective_at: z.string(),
});
export const windowSchema = z.object({
  id: z.uuid(),
  schedule_id: z.uuid(),
  applied_version: z.number().int(),
  local_date: z.string(),
  timezone: z.string(),
  starts_at: z.string(),
  cutoff_at: z.string(),
  ends_at: z.string(),
  expected_reporters: z.array(z.uuid()),
  reported_members: z.array(z.uuid()),
  coverage_state: z.enum(["NO_REPORTERS", "PARTIAL", "COMPLETE"]),
  full_coverage: z.boolean(),
  enabled: z.boolean(),
  roster_observed_at: z.string().nullable(),
  scope_changed: z.boolean(),
});
export const viewSchema = z.object({
  schedule: scheduleSchema.nullable(),
  window: windowSchema.nullable(),
  recipients: z.array(z.object({ membership_id: z.uuid(), name: z.string() })),
});
export type ScheduleCommand = z.infer<typeof commandSchema>;
export type ScheduleDraft = z.infer<typeof draftSchema>;
export type ScheduleView = z.infer<typeof viewSchema>;
