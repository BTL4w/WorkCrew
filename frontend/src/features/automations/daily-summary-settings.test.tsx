import { screen, fireEvent } from "@testing-library/react";
import { it, expect, vi } from "vitest";
import { renderWithAppProviders } from "@/test/render";
import { DailySummarySettings } from "./daily-summary-settings";

it("reviews a schedule before confirmation and can cancel without saving", async () => {
  const requests: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      requests.push(`${init?.method ?? "GET"} ${path}`);
      const command = {
        project_id: "11111111-1111-4111-8111-111111111111",
        timezone: "Asia/Ho_Chi_Minh",
        weekdays: [1, 2, 3, 4, 5],
        cutoff: "17:00",
        send_when_complete: true,
        partial_at_cutoff: true,
        recipients: ["22222222-2222-4222-8222-222222222222"],
      };
      const payload = path.endsWith("/preview")
        ? {
            id: "33333333-3333-4333-8333-333333333333",
            command,
            expected_version: 0,
            effective_at: "2026-10-04T00:00:00Z",
            expires_at: "2026-10-04T00:15:00Z",
          }
        : {
            schedule: null,
            window: null,
            recipients: [
              {
                membership_id: "22222222-2222-4222-8222-222222222222",
                name: "Manager",
              },
            ],
          };
      return new Response(JSON.stringify(payload), {
        headers: { "Content-Type": "application/json" },
      });
    }),
  );
  renderWithAppProviders(
    <DailySummarySettings
      projectId="11111111-1111-4111-8111-111111111111"
      organizationId="org"
      membershipId="m"
    />,
  );
  await screen.findByLabelText("Giờ chốt báo cáo");
  fireEvent.click(screen.getByLabelText("Manager"));
  fireEvent.click(screen.getByRole("button", { name: "Xem trước lịch" }));
  await screen.findByRole("heading", { name: "Xác nhận lịch tổng hợp" });
  expect(requests.some((value) => value.includes("/confirm"))).toBe(false);
  fireEvent.click(screen.getByRole("button", { name: "Quay lại chỉnh sửa" }));
  expect(
    screen.queryByRole("heading", { name: "Xác nhận lịch tổng hợp" }),
  ).not.toBeInTheDocument();
  vi.unstubAllGlobals();
});


it("edits a saved schedule using only editable fields and removes unavailable recipients", async () => {
  const manager = "22222222-2222-4222-8222-222222222222";
  const unavailable = "44444444-4444-4444-8444-444444444444";
  const command = {
    project_id: "11111111-1111-4111-8111-111111111111",
    timezone: "Asia/Ho_Chi_Minh", weekdays: [1,2,3,4,5], cutoff: "17:00",
    send_when_complete: true, partial_at_cutoff: true, recipients: [manager, unavailable],
  };
  let submitted: Record<string, unknown> | undefined;
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const preview = String(input).endsWith("/preview");
    if (preview) submitted = JSON.parse(String(init?.body)).command;
    return new Response(JSON.stringify(preview ? {
      id: "33333333-3333-4333-8333-333333333333", command: submitted,
      expected_version: 1, effective_at: "2026-10-05T00:00:00Z", expires_at: "2026-10-04T00:15:00Z",
    } : {
      schedule: {...command, id: "55555555-5555-4555-8555-555555555555", version: 1, paused: false, effective_at: "2026-10-04T00:00:00Z"},
      window: null, recipients: [{membership_id: manager, name: "Manager"}],
    }), {headers: {"Content-Type": "application/json"}});
  }));
  renderWithAppProviders(<DailySummarySettings projectId={command.project_id} organizationId="org" membershipId="m" />);
  await screen.findByLabelText("Giờ chốt báo cáo");
  fireEvent.change(screen.getByLabelText("Giờ chốt báo cáo"), {target: {value: "18:00"}});
  fireEvent.click(screen.getByRole("button", {name: "Xem trước lịch"}));
  await screen.findByRole("heading", {name: "Xác nhận lịch tổng hợp"});
  expect(Object.keys(submitted!).sort()).toEqual(Object.keys(command).sort());
  fireEvent.click(screen.getByRole("button", {name: "Quay lại chỉnh sửa"}));
  fireEvent.click(screen.getByRole("button", {name: "Loại bỏ người nhận không còn quyền"}));
  fireEvent.click(screen.getByRole("button", {name: "Xem trước lịch"}));
  await screen.findByRole("heading", {name: "Xác nhận lịch tổng hợp"});
  expect(submitted?.recipients).toEqual([manager]);
  vi.unstubAllGlobals();
});
