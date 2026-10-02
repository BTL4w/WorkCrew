import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { AppLocaleProvider } from "@/shared/i18n/locale-provider";
import { EvidencePicker } from "./evidence-picker";

const evidence = {
  evidence_id: "11111111-1111-4111-8111-111111111111", version: 1,
  filename: "proof.png", mime_type: "image/png", byte_length: 4,
  sha256: "a".repeat(64), uploaded_at: "2026-09-29T00:00:00Z",
  expires_at: "2026-10-06T00:00:00Z",
};
afterEach(() => vi.unstubAllGlobals());
it("uploads a selected original and provides an authenticated download action", async () => {
  const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(evidence), {
    status: 201, headers: { "Content-Type": "application/json" },
  }));
  vi.stubGlobal("fetch", fetchMock);
  const onUploaded = vi.fn();
  render(<AppLocaleProvider initialLocale="en"><EvidencePicker onUploaded={onUploaded} /></AppLocaleProvider>);
  fireEvent.change(screen.getByLabelText("Choose evidence"), {
    target: { files: [new File(["test"], "proof.png", { type: "image/png" })] },
  });
  await waitFor(() => expect(onUploaded).toHaveBeenCalledWith(evidence));
  expect(screen.getByRole("link", { name: /Download/ })).toHaveAttribute(
    "href", `/api/v1/evidence/${evidence.evidence_id}/versions/1/content`,
  );
  expect(screen.queryByRole("button", { name: "Preview evidence" })).not.toBeInTheDocument();
  expect(fetchMock.mock.calls[0][1].credentials).toBe("include");
  expect(fetchMock.mock.calls[0][1].headers["Idempotency-Key"]).toBeTruthy();
});
it("rejects an oversized file before sending bytes", () => {
  const fetchMock = vi.fn(); vi.stubGlobal("fetch", fetchMock);
  render(<AppLocaleProvider initialLocale="en"><EvidencePicker /></AppLocaleProvider>);
  const file = new File(["x"], "proof.png", { type: "image/png" });
  Object.defineProperty(file, "size", { value: 20 * 1024 * 1024 + 1 });
  fireEvent.change(screen.getByLabelText("Choose evidence"), { target: { files: [file] } });
  expect(screen.getByRole("alert")).toHaveTextContent("20 MiB");
  expect(fetchMock).not.toHaveBeenCalled();
});

it("keeps the idempotency key for a network retry", async () => {
  const fetchMock = vi.fn()
    .mockRejectedValueOnce(new TypeError("network interrupted"))
    .mockResolvedValueOnce(new Response(JSON.stringify(evidence), {
      status: 201, headers: { "Content-Type": "application/json" },
    }));
  vi.stubGlobal("fetch", fetchMock);
  render(<AppLocaleProvider initialLocale="en"><EvidencePicker /></AppLocaleProvider>);
  fireEvent.change(screen.getByLabelText("Choose evidence"), {
    target: { files: [new File(["test"], "proof.png", { type: "image/png" })] },
  });
  await waitFor(() => expect(screen.getByRole("button", { name: "Retry upload" })).toBeVisible());
  fireEvent.click(screen.getByRole("button", { name: "Retry upload" }));
  await waitFor(() => expect(screen.getByRole("link", { name: "Download" })).toBeVisible());
  expect(fetchMock.mock.calls[0][1].headers["Idempotency-Key"]).toBe(
    fetchMock.mock.calls[1][1].headers["Idempotency-Key"],
  );
});

it("shows a Vietnamese validation message for unsupported file types", () => {
  const fetchMock = vi.fn(); vi.stubGlobal("fetch", fetchMock);
  render(<AppLocaleProvider initialLocale="vi"><EvidencePicker /></AppLocaleProvider>);
  fireEvent.change(screen.getByLabelText("Chọn bằng chứng"), {
    target: { files: [new File(["test"], "data.xlsx")] },
  });
  expect(screen.getByRole("alert")).toHaveTextContent("Chọn tệp PDF, DOCX, JPG hoặc PNG hợp lệ.");
  expect(fetchMock).not.toHaveBeenCalled();
});
