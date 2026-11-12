import { expect, test } from "@playwright/test";

for (const locale of ["vi", "en"] as const) {
  test(`${locale}: workload reload reflects a committed capacity change`, async ({ page, playwright }) => {
    await page.clock.setFixedTime(new Date("2026-11-13T09:00:00+07:00"));
    await page.addInitScript((language) => {
      localStorage.setItem("work-management-locale", language);
    }, locale);
    await page.goto("/login");
    await page.getByLabel("Email").fill("bench-0-1@example.test");
    await page.getByLabel(locale === "vi" ? "Mật khẩu" : "Password").fill("RedisBenchmarkOnly-2026!");
    const loginResponse = page.waitForResponse((response) => response.url().endsWith("/api/v1/auth/login"));
    await page.getByRole("button", { name: locale === "vi" ? "Đăng nhập" : "Sign in", exact: true }).click();
    expect((await loginResponse).status()).toBe(200);
    await page.goto("/?view=peopleCapacity");
    const workload = page.locator(".availability-workload").first();
    await expect(workload.locator(".availability-allocation strong")).toContainText("/ 40");
    // Revisit the real UI to warm the backend cache without intercepting any API.
    await page.reload();
    await expect(workload.locator(".availability-allocation strong")).toContainText("/ 40");
    const employee = await page.request.get("/api/v1/me");
    expect(employee.status()).toBe(200);
    const membership = (await employee.json()).membership.id;
    const manager = await playwright.request.newContext({ baseURL: "http://127.0.0.1:8117" });
    const login = await manager.post("/api/v1/auth/login", { data: {
      email: "bench-0-0@example.test", password: "RedisBenchmarkOnly-2026!",
    } });
    expect(login.status()).toBe(200);
    const capacity = (await (await manager.get(`/api/v1/capacity?membership_id=${membership}`)).json())[0];
    const payload = { membership_id: membership, kind: "DEFAULT", hours: 39,
      effective_from: "2026-11-09", effective_to: "2027-11-08" };
    const changed = await manager.post("/api/v1/capacity", { data: payload,
      headers: { "Idempotency-Key": crypto.randomUUID(), "If-Match": `"${capacity.version}"` } });
    expect(changed.status()).toBe(200);
    try {
      await page.reload();
      await expect(workload.locator(".availability-allocation strong")).toContainText("/ 39");
    } finally {
      const updated = await changed.json();
      const restored = await manager.post("/api/v1/capacity", { data: { ...payload, hours: 40 },
        headers: { "Idempotency-Key": crypto.randomUUID(), "If-Match": `"${updated.version}"` } });
      expect(restored.status()).toBe(200);
      await manager.dispose();
    }
  });
}
