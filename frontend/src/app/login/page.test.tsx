import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  authError,
  jsonResponse,
  managerActor,
  renderWithAppProviders,
} from "@/test/render";

import LoginPage from "./page";

const navigation = vi.hoisted(() => ({ replace: vi.fn() }));

vi.mock("next/navigation", () => ({
  useRouter: () => navigation,
}));

describe("LoginPage", () => {
  beforeEach(() => {
    navigation.replace.mockReset();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders an anonymous session and validates fields before login", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse(authError("AUTHENTICATION_REQUIRED"), 401));
    vi.stubGlobal("fetch", fetchMock);

    renderWithAppProviders(<LoginPage />);
    const email = await screen.findByLabelText("Email");
    fireEvent.change(email, { target: { value: "invalid" } });
    fireEvent.click(screen.getByRole("button", { name: "Đăng nhập" }));

    expect(await screen.findByText("Nhập một địa chỉ email hợp lệ.")).toBeVisible();
    expect(screen.getByText("Mật khẩu không được để trống.")).toBeVisible();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("shows the generic invalid-credentials message", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(authError("AUTHENTICATION_REQUIRED"), 401))
      .mockResolvedValueOnce(jsonResponse(authError("INVALID_CREDENTIALS"), 401));
    vi.stubGlobal("fetch", fetchMock);

    renderWithAppProviders(<LoginPage />);
    fireEvent.change(await screen.findByLabelText("Mật khẩu"), {
      target: { value: "wrong-password" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Đăng nhập" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Email hoặc mật khẩu không đúng.",
    );
  });

  it.each(["vi", "en"] as const)("%s explains login throttling and keeps entered fields", async (locale) => {
    vi.stubGlobal("fetch", vi.fn()
      .mockResolvedValueOnce(jsonResponse(authError("AUTHENTICATION_REQUIRED"), 401))
      .mockResolvedValueOnce(jsonResponse({ error: { code: "LOGIN_RATE_LIMITED", message_key: "auth.login.rateLimited", request_id: "safe-reference", details: { retry_after_seconds: 12 } } }, 429)));
    renderWithAppProviders(<LoginPage />, locale);
    const email = await screen.findByLabelText("Email");
    const password = screen.getByLabelText(locale === "vi" ? "Mật khẩu" : "Password");
    fireEvent.change(email, { target: { value: "user@example.test" } });
    fireEvent.change(password, { target: { value: "test-password" } });
    fireEvent.click(screen.getByRole("button", { name: locale === "vi" ? "Đăng nhập" : "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(locale === "vi" ? "Quá nhiều lần thử đăng nhập. Hãy thử lại sau 12 giây." : "Too many sign-in attempts. Try again in 12 seconds.");
    expect(email).toHaveValue("user@example.test");
    expect(password).toHaveValue("test-password");
  });

  it("logs in through the same-origin API and redirects home", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(authError("AUTHENTICATION_REQUIRED"), 401))
      .mockResolvedValueOnce(jsonResponse(managerActor));
    vi.stubGlobal("fetch", fetchMock);

    renderWithAppProviders(<LoginPage />);
    fireEvent.change(await screen.findByLabelText("Mật khẩu"), {
      target: { value: "WorkDemo123!" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Đăng nhập" }));

    await waitFor(() => expect(navigation.replace).toHaveBeenCalledWith("/"));
    expect(fetchMock).toHaveBeenLastCalledWith(
      "/api/v1/auth/login",
      expect.objectContaining({
        method: "POST",
        credentials: "include",
        body: JSON.stringify({
          email: "manager@example.test",
          password: "WorkDemo123!",
        }),
      }),
    );
  });

  it("explains when the previous session expired", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse(authError("SESSION_EXPIRED"), 401)),
    );

    renderWithAppProviders(<LoginPage />);

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Phiên đăng nhập đã hết hạn. Vui lòng đăng nhập lại.",
    );
  });
});
