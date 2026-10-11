import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { jsonResponse, managerActor, renderWithAppProviders } from "@/test/render";
import { getMessages } from "@/shared/i18n/messages";

import HomePage from "./page";

const navigation = vi.hoisted(() => ({ replace: vi.fn() }));

vi.mock("next/navigation", () => ({
  useRouter: () => navigation,
  useSearchParams: () => new URLSearchParams(globalThis.location.search),
}));

describe("HomePage", () => {
  beforeEach(() => {
    navigation.replace.mockReset();
    globalThis.history.replaceState({}, "", "/");
  });

  afterEach(() => {
    globalThis.history.replaceState({}, "", "/");
    vi.unstubAllGlobals();
  });

  it.each(["vi", "en"] as const)("renders the verified actor in the %s locale", async (locale) => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      if (String(input) === "/api/v1/me") return jsonResponse(managerActor);
      if (String(input) === "/api/v1/projects") return jsonResponse({ items: [], page: 1, page_size: 20, total: 0 });
      throw new Error(`Unexpected request: ${String(input)}`);
    }));

    renderWithAppProviders(<HomePage />, locale);

    expect(await screen.findByText("Demo Manager")).toBeVisible();
    expect(screen.getByText(`${getMessages(locale).work.role.MANAGER} · ${managerActor.membership.organization_name}`)).toBeVisible();
    expect(screen.getByText("manager@example.test")).toBeVisible();
    expect(screen.getByRole("heading", { name: getMessages(locale).assistant.title })).toBeVisible();
  });

  it("revokes the session and redirects to login", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input);
      if (path === "/api/v1/me") return jsonResponse(managerActor);
      if (path === "/api/v1/projects") return jsonResponse({ items: [], page: 1, page_size: 20, total: 0 });
      if (path === "/api/v1/auth/logout" && init?.method === "POST") return new Response(null, { status: 204 });
      throw new Error(`Unexpected request: ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    renderWithAppProviders(<HomePage />);
    fireEvent.click(await screen.findByRole("button", { name: "Đăng xuất" }));

    await waitFor(() => expect(navigation.replace).toHaveBeenCalledWith("/login"));
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/auth/logout",
      expect.objectContaining({ method: "POST", credentials: "include" }),
    );
  });
});
