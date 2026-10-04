import { requestJson } from "@/shared/api/client";
import { reportSourcesSchema, reportDefaultsSchema, reportPageSchema, reportResultSchema, type ReportInput } from "./contracts";

export function listReports(projectId: string, page = 1) {
  return requestJson(`/api/v1/reports?project_id=${encodeURIComponent(projectId)}&page=${page}`, { schema: reportPageSchema });
}
export function getReport(id: string) {
  return requestJson(`/api/v1/reports/${encodeURIComponent(id)}`, { schema: reportResultSchema });
}
export function createReport(body: ReportInput, key: string) {
  return requestJson("/api/v1/reports", { schema: reportResultSchema, expectedStatus: 201, init: {
    method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": key }, body: JSON.stringify(body),
  } });
}

export function getReportDefaults(projectId: string) {
  return requestJson(`/api/v1/reports/defaults?project_id=${encodeURIComponent(projectId)}`, { schema: reportDefaultsSchema });
}

export function getReportSources(id: string, cursor?: string) {
  const query = new URLSearchParams({ page_size: "20" });
  if (cursor) query.set("cursor", cursor);
  return requestJson(`/api/v1/reports/${encodeURIComponent(id)}/sources?${query}`, { schema: reportSourcesSchema });
}
