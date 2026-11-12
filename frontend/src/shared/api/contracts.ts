import { z } from "zod";

export const membershipRoleSchema = z.enum(["ADMIN", "MANAGER", "EMPLOYEE"]);

export const meResponseSchema = z.object({
  user: z.object({
    id: z.uuid(),
    email: z.string(),
    display_name: z.string(),
  }),
  membership: z.object({
    id: z.uuid(),
    organization_id: z.uuid(),
    organization_name: z.string(),
    role: membershipRoleSchema,
  }),
});

export type MeResponse = z.infer<typeof meResponseSchema>;

export const fieldErrorSchema = z.object({
  field: z.string(),
  code: z.string(),
  message_key: z.string(),
});

export const errorDetailSchema = z.object({
    code: z.string(),
    message_key: z.string(),
    request_id: z.string(),
    field_errors: z.array(fieldErrorSchema).default([]),
    details: z.record(z.string(), z.unknown()).default({}),
});

export const errorResponseSchema = z.object({
  error: errorDetailSchema,
});

export const admissionRetryDetailsSchema = z.object({
  retry_after_seconds: z.number().int().min(1).max(60),
});
