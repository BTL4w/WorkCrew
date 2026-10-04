import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import { AppLocaleProvider } from "@/shared/i18n/locale-provider";
import { ReportPanel } from "./report-panel";

const id = "11111111-1111-4111-8111-111111111111";
const time = "2026-10-04T09:00:00Z";
const metric = (key: string, value: string | null) => ({ key, value, unit: "COUNT", state: value === null ? "UNKNOWN" : "KNOWN", time_basis: "AT_CAPTURE", policy_version: "report-metrics.v1", source_refs: [], limitations: [] });
const result = {
  report: { id, organization_id: id, project_id: id, kind: "DAILY", locale: "en", version: 1, snapshot_id: id, selected_version_id: id, created_by_membership_id: id, narrative_requested: true, created_at: time },
  snapshot: { id, organization_id: id, project_id: id, report_id: id, captured_at: time, snapshot_hash: "a".repeat(64), catalog_version: "report-metrics.v1", query_version: "report-sql.v1", period: { kind: "DAILY", local_start: "2026-10-04", local_end: "2026-10-05", timezone: "UTC", start_utc: "2026-10-04T00:00:00Z", end_utc: "2026-10-05T00:00:00Z", observed_through: time, partial_period: true }, metrics: { "tasks.status.total_count": metric("tasks.status.total_count", "1"), "tasks.status.done_count": metric("tasks.status.done_count", "0"), "progress.observation_coverage": metric("progress.observation_coverage", null) }, sources: [], receipts: [], limitations: [] },
  selected_version: { id, report_id: id, snapshot_id: id, origin: "METRICS_ONLY", locale: "en", created_at: time }, publications: [], generation_state: "AI_UNAVAILABLE", replayed: false,
};
afterEach(() => { vi.unstubAllGlobals(); localStorage.clear(); });

it.each(["en", "vi"] as const)("creates and displays captured daily metrics in %s with explicit unknowns", async locale => {
  const fetch = vi.fn(async (path: string, init?: RequestInit) => new Response(JSON.stringify(path.includes("/defaults?") ? {timezone: "UTC", period_start: "2026-10-04"} : init?.method === "POST" || path.includes(`${id}?`) || path.endsWith(`/${id}`) ? result : { items: [], page: 1, page_size: 20, total: 0 }), { status: init?.method === "POST" ? 201 : 200, headers: { "Content-Type": "application/json" } }));
  vi.stubGlobal("fetch", fetch);
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><AppLocaleProvider initialLocale={locale}><ReportPanel projectId={id} organizationId={id} actorMembershipId={id} /></AppLocaleProvider></QueryClientProvider>);
  fireEvent.click(await screen.findByRole("button", { name: locale === "en" ? "Create report" : "Tạo báo cáo" }));
  expect(await screen.findByDisplayValue("UTC")).toBeVisible();
  expect(screen.getByDisplayValue("2026-10-04")).toBeVisible();
  fireEvent.click(screen.getByRole("button", { name: locale === "en" ? "Generate report" : "Tạo báo cáo số liệu" }));
  await waitFor(() => expect(fetch.mock.calls.some(([, init]) => init?.method === "POST")).toBe(true));
  expect(await screen.findByText(locale === "en" ? "Unknown" : "Chưa xác định")).toBeVisible();
  expect(screen.getByText(locale === "en" ? "AI commentary unavailable. Your metrics remain available." : "Nhận xét AI chưa khả dụng. Bạn vẫn có thể sử dụng số liệu.")).toBeVisible();
  expect(screen.getByText("UTC", { exact: false })).toBeVisible();
});
