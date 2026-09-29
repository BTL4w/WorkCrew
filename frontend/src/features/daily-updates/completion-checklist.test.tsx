import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, expect, it, vi } from "vitest";
import { AppLocaleProvider } from "@/shared/i18n/locale-provider";
import { CompletionChecklist } from "./completion-checklist";

const taskId = "11111111-1111-4111-8111-111111111111";
const criterionId = "22222222-2222-4222-8222-222222222222";
afterEach(() => vi.unstubAllGlobals());

it("requires current criterion confirmation and sends exact versions", async () => {
  const fetchMock = vi.fn(async (path: string, options?: RequestInit) => {
    if (path.includes("acceptance-criteria")) return new Response(JSON.stringify({
      items: [{ id: criterionId, task_id: taskId, text: "Check output", position: 1, version: 2,
        created_at: "2026-09-29T00:00:00Z", updated_at: "2026-09-29T00:00:00Z" }],
      page: 1, page_size: 100, total: 1,
    }), { headers: { "Content-Type": "application/json" } });
    if (options?.method === "POST") return new Response(JSON.stringify({}), {
      headers: { "Content-Type": "application/json" },
    });
    throw new Error(`unexpected ${path}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <AppLocaleProvider initialLocale="en"><CompletionChecklist taskId={taskId} version={1} organizationId="org" actorMembershipId="member"
      isEmployee={false} onCompleted={vi.fn()} /></AppLocaleProvider>
  </QueryClientProvider>);
  const complete = await screen.findByRole("button", { name: "Complete" });
  expect(complete).toBeDisabled();
  fireEvent.click(await screen.findByLabelText("Check output"));
  fireEvent.click(complete);
  await waitFor(() => expect(fetchMock.mock.calls.some(([path, options]) =>
    String(path).endsWith("/status") && options?.method === "POST")).toBe(true));
  const call = fetchMock.mock.calls.find(([path]) => String(path).endsWith("/status"));
  expect(JSON.parse(String(call?.[1]?.body))).toEqual({ to_status: "DONE", attestations: [
    { criterion_id: criterionId, version: 2, confirmed: true, evidence_refs: [] },
  ] });
});

it("guides an employee to report before completion", async () => {
  vi.stubGlobal("fetch", vi.fn(async (path: string) => new Response(JSON.stringify(
    path.includes("acceptance-criteria")
      ? { items: [], page: 1, page_size: 100, total: 0 }
      : { task_id: taskId, task_version: 1, progress_version: 0, reported_percent: null,
        remaining_hours: null, reporting_timezone: "UTC", reporting_date: "2026-09-29",
        project_week_state: "NO_PROJECT_WEEK", evidence_refs: [] },
  ), { headers: { "Content-Type": "application/json" } })));
  render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <AppLocaleProvider initialLocale="en"><CompletionChecklist taskId={taskId} version={1} organizationId="org" actorMembershipId="member"
      isEmployee onCompleted={vi.fn()} /></AppLocaleProvider>
  </QueryClientProvider>);
  expect(await screen.findByText(/confirm a 100% report/)).toBeVisible();
  expect(screen.getByRole("link", { name: "Open daily update" })).toHaveAttribute("href", "#daily-update-form");
  expect(screen.getByRole("button", { name: "Complete" })).toBeDisabled();
});
