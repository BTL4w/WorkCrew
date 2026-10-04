import { fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, expect, it, vi } from "vitest";
import { AppLocaleProvider } from "@/shared/i18n/locale-provider";
import { SourceList } from "./source-list";

const id = "11111111-1111-4111-8111-111111111111";
afterEach(() => vi.unstubAllGlobals());
it.each(["vi", "en"] as const)("shows captured source identity and freshness in %s", async locale => {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ snapshot_hash: "a".repeat(64), items: [{ source: { resource_type: "TASK", resource_id: id, version: 1, fingerprint: "a".repeat(64), observed_at: "2026-10-04T12:00:00Z", label: "Original survey", facts: {remaining_hours:"12",reporting_timezone:"Asia/Ho_Chi_Minh"} }, freshness: "UPDATED" }], receipts: [], next_cursor: null, total: 1 }), { headers: { "Content-Type": "application/json" } })));
  render(<QueryClientProvider client={new QueryClient({defaultOptions:{queries:{retry:false}}})}><AppLocaleProvider initialLocale={locale}><SourceList reportId={id} organizationId={id} actorMembershipId={id} /></AppLocaleProvider></QueryClientProvider>);
  expect(await screen.findByText("Original survey")).toBeVisible();
  expect(screen.getByText(locale === "en" ? "Changed since capture" : "Đã thay đổi sau khi chụp")).toBeVisible();
  expect(screen.getByText("v1", {exact:false})).toBeVisible();
  fireEvent.click(screen.getByText(locale === "en" ? "Captured details" : "Chi tiết đã chụp"));
  expect(screen.getByText("Asia/Ho_Chi_Minh")).toBeVisible();
  expect(screen.getByText(locale === "en" ? "Remaining hours" : "Giờ còn lại")).toBeVisible();
});
