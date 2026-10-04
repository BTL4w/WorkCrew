import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { expect, it, vi } from "vitest";
import { AppLocaleProvider } from "@/shared/i18n/locale-provider";
import { PublicationHistory } from "./publication-history";
import type { ReportResult } from "./contracts";

const id = "11111111-1111-4111-8111-111111111111";
const release = "22222222-2222-4222-8222-222222222222";
const hash = "a".repeat(64);
// Only identity/snapshot fields used by this component are needed for the fixture.
const data = { report: { id, version: 1, current_publication_id: null }, selected_version: { id }, snapshot: { snapshot_hash: hash, metrics: {}, period: { timezone: "UTC" } }, publications: [] } as unknown as ReportResult;
it.each(["en", "vi"] as const)("publishes the exact version and displays a separate immutable history in %s", async locale => {
  const publication = { id: release, report_id: id, report_version_id: id, snapshot_hash: hash, publisher_membership_id: id, decision_id: release, published_at: "2026-10-05T09:00:00Z" };
  const published = { ...data, report: { ...data.report, version: 2, current_publication_id: release }, publications: [publication] };
  const publish = vi.fn().mockResolvedValue(published);
  const onPublished = vi.fn();
  const onStale = vi.fn();
  render(<QueryClientProvider client={new QueryClient()}><AppLocaleProvider initialLocale={locale}><PublicationHistory data={data} publish={publish} onPublished={onPublished} onStale={onStale} /></AppLocaleProvider></QueryClientProvider>);
  fireEvent.click(screen.getByRole("button", {name: locale === "en" ? "Publish metrics only" : "Xuất bản chỉ số liệu"}));
  await waitFor(() => expect(onPublished).toHaveBeenCalledWith(published));
  expect(publish).toHaveBeenCalledWith(id, {mode: "METRICS_ONLY", report_version_id: id, snapshot_hash: hash}, 1, expect.any(String));
  expect(onStale).not.toHaveBeenCalled();
});
it("keeps the stale action blocked while refreshing the report", async () => {
  const onStale = vi.fn();
  const publish = vi.fn().mockRejectedValue({ status: 412 });
  render(<QueryClientProvider client={new QueryClient()}><AppLocaleProvider initialLocale="en"><PublicationHistory data={data} publish={publish} onPublished={vi.fn()} onStale={onStale} /></AppLocaleProvider></QueryClientProvider>);
  fireEvent.click(screen.getByRole("button", {name: "Publish metrics only"}));
  expect(await screen.findByText("This report changed. Review the refreshed version before publishing.")).toBeVisible();
  expect(screen.getByRole("button", {name: "Publish metrics only"})).toBeDisabled();
  expect(onStale).toHaveBeenCalledOnce();
});
it("opens a historical publication without moving the draft selection", () => {
  const publication = { id: release, report_id: id, report_version_id: id, snapshot_hash: hash, publisher_membership_id: id, decision_id: release, published_at: "2026-10-05T09:00:00Z" };
  render(<QueryClientProvider client={new QueryClient()}><AppLocaleProvider initialLocale="en"><PublicationHistory data={{...data, publications:[publication]}} publish={vi.fn()} onPublished={vi.fn()} onStale={vi.fn()} /></AppLocaleProvider></QueryClientProvider>);
  fireEvent.click(screen.getByRole("button", {name: /View published version/}));
  expect(screen.getByRole("region", {name: "Published metrics"})).toHaveTextContent(hash);
  expect(screen.getByRole("region", {name: "Published metrics"})).toHaveTextContent(id);
});
it("retries an uncertain publication with the same idempotency key", async () => {
  const publish = vi.fn().mockRejectedValueOnce(new TypeError("Connection lost")).mockResolvedValueOnce(data);
  const onPublished = vi.fn();
  render(<QueryClientProvider client={new QueryClient()}><AppLocaleProvider initialLocale="en"><PublicationHistory data={data} publish={publish} onPublished={onPublished} onStale={vi.fn()} /></AppLocaleProvider></QueryClientProvider>);
  fireEvent.click(screen.getByRole("button", {name: "Publish metrics only"}));
  expect(await screen.findByText("Publication could not be confirmed. Retry to check the same request.")).toBeVisible();
  fireEvent.click(screen.getByRole("button", {name: "Publish metrics only"}));
  await waitFor(() => expect(onPublished).toHaveBeenCalledWith(data));
  expect(publish.mock.calls[0][3]).toBe(publish.mock.calls[1][3]);
});
