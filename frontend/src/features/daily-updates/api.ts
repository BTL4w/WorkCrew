import { requestJson } from "@/shared/api/client";
import { evidenceSchema, evidenceTypes, type Evidence } from "./contracts";

export function uploadEvidence(file: File, idempotencyKey: string) {
  const extension = file.name.split(".").pop()?.toLowerCase() ?? "";
  return requestJson("/api/v1/evidence", {
    schema: evidenceSchema, expectedStatus: 201,
    init: {
      method: "POST", body: file,
      headers: {
        "Content-Type": evidenceTypes[extension] ?? file.type,
        "X-Evidence-Filename": encodeURIComponent(file.name),
        "Idempotency-Key": idempotencyKey,
      },
    },
  });
}
export function evidenceDownloadUrl(evidence: Evidence) {
  return `/api/v1/evidence/${evidence.evidence_id}/versions/${evidence.version}/content`;
}
