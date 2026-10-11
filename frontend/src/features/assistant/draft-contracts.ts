import { z } from "zod";

export const planningFieldSchema = z
  .object({
    value: z
      .union([z.string(), z.number().int(), z.array(z.string())])
      .nullable(),
    status: z.enum([
      "USER_STATED",
      "AUTHORIZED_SOURCE",
      "AI_ASSUMPTION",
      "UNKNOWN",
      "CONFLICTING",
    ]),
    source_refs: z.array(z.string()).default([]),
    assumption_reason: z.string().nullable().optional(),
  })
  .strict();
const phaseSchema = z
  .object({
    ref: z.string(),
    title: z.string(),
    start_day: z.number().int().nonnegative(),
    end_day: z.number().int().positive(),
  })
  .strict();
const conflictSchema = z
  .object({
    code: z.string(),
    requested_days: z.number().int(),
    minimum_days: z.number().int(),
    chain_refs: z.array(z.string()),
    estimated: z.boolean(),
  })
  .strict();
export const planningDraftSnapshotSchema = z
  .object({
    draft_id: z.uuid(),
    workflow_run_id: z.uuid(),
    draft_version: z.number().int().positive(),
    current_version: z.number().int().positive(),
    fingerprint: z.string().regex(/^[a-f0-9]{64}$/),
    can_prepare: z.boolean().default(true),
    suggested_anchor_date: z.iso.date().nullable().default(null),
    content: z
      .object({
        schema_version: z.literal("planning-draft.v1"),
        brief: z
          .object({
            objective: planningFieldSchema,
            audience: planningFieldSchema,
            timing: planningFieldSchema,
            budget: z
              .object({
                amount: z.number().int().nonnegative().nullable(),
                currency: z.string(),
                status: z.string(),
                source_refs: z.array(z.string()),
                assumption_reason: z.string().nullable().optional(),
              })
              .nullable()
              .optional(),
          })
          .passthrough(),
        duration_days: z.number().int().positive(),
        phases: z.array(phaseSchema),
        tasks: z.array(
          z
            .object({
              ref: z.string(),
              title: z.string(),
              start_day: z.number().int(),
              end_day: z.number().int(),
              duration_days: z.number().int(),
              duration_source: z.string(),
            })
            .passthrough(),
        ),
        dependencies: z.array(
          z
            .object({ predecessor_ref: z.string(), successor_ref: z.string() })
            .strict(),
        ),
        assumptions: z.array(planningFieldSchema),
        open_questions: z.array(z.object({ text: z.string() }).passthrough()),
      })
      .strict(),
    question: z.object({ text: z.string() }).passthrough().nullable(),
    validation: z
      .object({
        errors: z.array(
          z.object({ path: z.string(), code: z.string() }).strict(),
        ),
        warnings: z
          .array(z.object({ path: z.string(), code: z.string() }).strict())
          .optional(),
        conflicts: z.array(conflictSchema),
      })
      .strict(),
    versions: z.record(z.string(), z.string()),
    change_summary: z.string().nullable(),
  })
  .strict();
export type PlanningDraftSnapshot = z.infer<typeof planningDraftSnapshotSchema>;
