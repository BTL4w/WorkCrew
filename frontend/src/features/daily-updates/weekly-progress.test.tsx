import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import { AppLocaleProvider } from "@/shared/i18n/locale-provider";
import { WeeklyProgressView } from "./weekly-progress";

const id = "11111111-1111-4111-8111-111111111111";
const baseline = { id, project_week_id: id, captured_at: "2026-09-28T00:00:00Z", kind: "ENTRY", start_date: "2026-09-28", end_date: "2026-10-02", week_version: 1, sealed_at: null, task_entries: [{ task_id: id, task_version: 1, title: "First", effort_hours: "2", due_date: null, predecessor_ids: [] }] };
const metrics = { baseline, evaluated_at: "2026-09-29T00:00:00Z", calendar_version: "mon-fri-utc-v1", reported_percent: "50", planned_percent: "40", known_effort_fraction: "0.25", known_effort_hours: "2", total_effort_hours: "8", task_coverage_fraction: "0.5", unknown_progress_ids: [id], missing_estimate_ids: [id], stale_task_ids: [id], remaining_hours: null, unknown_remaining_ids: [id], task_actuals: [{task_id: id, progress_version: 0, reported_percent: null, remaining_hours: null, observation_id: null, reporting_at: null, status: "TO_DO", stale: false}] };
it("labels partial progress and shows coverage separately from full-week completion", () => {
  render(<AppLocaleProvider initialLocale="en"><WeeklyProgressView data={{original: metrics, current_plan: metrics, added_task_ids: [id], removed_task_ids: [], sealed: false}} /></AppLocaleProvider>);
  expect(screen.getAllByText(/Progress of reported effort: 50%/)).toHaveLength(2);
  expect(screen.getAllByText(/Effort coverage: 25%/)).toHaveLength(2);
  expect(screen.getAllByText(/Missing estimates: 1/)).toHaveLength(2);
  expect(screen.getAllByText(/Stale reports: 1/)).toHaveLength(2);
  expect(screen.getByText(/Added tasks: 1/)).toBeVisible();
  expect(screen.getAllByText(/Phase 4 entry baseline/)).toHaveLength(2);
});
it("shows Vietnamese unknown values and sealed history without fabricating zero progress", () => {
  const unknown = {...metrics, reported_percent: null, known_effort_fraction: null};
  render(<AppLocaleProvider initialLocale="vi"><WeeklyProgressView data={{original: unknown, current_plan: unknown, added_task_ids: [], removed_task_ids: [], sealed: true}} /></AppLocaleProvider>);
  expect(screen.getByText("Lịch sử đã chốt")).toBeVisible();
  expect(screen.getAllByText(/Tiến độ phần đã báo cáo: Chưa xác định/)).toHaveLength(2);
});

it("falls back to an available week when the selected empty week is deleted", async () => {
  const { QueryClient, QueryClientProvider } = await import("@tanstack/react-query");
  const { fireEvent, waitFor } = await import("@testing-library/react");
  const { vi } = await import("vitest");
  const { WeeklyProgressPanel } = await import("./weekly-progress");
  const second = "22222222-2222-4222-8222-222222222222";
  const third = "33333333-3333-4333-8333-333333333333";
  let available = [id, second, third];
  const fetchMock = vi.fn(async (url: string | URL | Request) => {
    const path = String(url);
    const result = path.includes("/progress") ? {original: metrics, current_plan: metrics, added_task_ids: [], removed_task_ids: [], sealed: false} : {items: available.map((weekId, index) => ({id: weekId, organization_id: id, project_id: id, week_number: index + 1, start_date: "2026-09-28", end_date: "2026-10-02", objective: "Deliver", status: "PLANNED", version: 1, created_at: "2026-09-28T00:00:00Z", updated_at: "2026-09-28T00:00:00Z"})), total: available.length, page: 1, page_size: 100};
    return new Response(JSON.stringify(result), {headers: {"Content-Type": "application/json"}});
  });
  vi.stubGlobal("fetch", fetchMock);
  const client = new QueryClient({defaultOptions: {queries: {retry: false}}});
  try {
    render(<QueryClientProvider client={client}><AppLocaleProvider initialLocale="en"><WeeklyProgressPanel organizationId={id} actorMembershipId={id} projectId={id} /></AppLocaleProvider></QueryClientProvider>);
    await waitFor(() => expect(screen.getByRole("option", {name: "Week 2"})).toBeVisible());
    fireEvent.change(screen.getByLabelText("Progress week"), {target: {value: second}});
    await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url).includes(`/weeks/${second}/progress`))).toBe(true));
    available = [id, third];
    fetchMock.mockClear();
    await client.invalidateQueries({queryKey: ["work", id, id, "weekly-progress", id]});
    await waitFor(() => expect(screen.getByLabelText("Progress week")).toHaveValue(id));
    await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url).includes(`/weeks/${id}/progress`))).toBe(true));
  } finally { client.clear(); vi.unstubAllGlobals(); }
});
