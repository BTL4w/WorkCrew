import { z } from "zod";

export const evidenceSchema = z.object({
  evidence_id: z.uuid(), version: z.number().int().positive(),
  filename: z.string(), mime_type: z.string(),
  byte_length: z.number().int().positive().max(20 * 1024 * 1024),
  sha256: z.string().regex(/^[0-9a-f]{64}$/),
  uploaded_at: z.iso.datetime({ offset: true }),
  expires_at: z.iso.datetime({ offset: true }).nullable(),
});
export type Evidence = z.infer<typeof evidenceSchema>;
export const MAX_EVIDENCE_BYTES = 20 * 1024 * 1024;
export const evidenceTypes: Record<string, string> = {
  pdf: "application/pdf",
  docx: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  jpg: "image/jpeg", jpeg: "image/jpeg", png: "image/png",
};
