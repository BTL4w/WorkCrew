import { expect, test } from "@playwright/test";

for (const locale of ["vi", "en"] as const) {
  test(`Manager creates immutable daily project report in ${locale}`, async ({ page }) => {
    await page.goto("/login");
    await page.getByLabel("Email").fill("manager@example.test");
    await page.getByLabel("Mật khẩu").fill("WorkDemo123!");
    await page.getByRole("button", { name: "Đăng nhập" }).click();
    await expect(page.getByRole("button", { name: "Đăng xuất" })).toBeVisible();
    if (locale === "en") await page.getByRole("button", { name: "en", exact: true }).click();
    const name = `Report ${locale} ${Date.now()}`;
    const headers = () => ({ "Idempotency-Key": crypto.randomUUID() });
    const created = await page.request.post("/api/v1/projects", { data: { name }, headers: headers() });
    expect(created.status()).toBe(201);
    const project = await created.json();
    const weekResponse = await page.request.post(`/api/v1/projects/${project.id}/weeks`, {
      data: { week_number: 1, start_date: "2026-09-28", end_date: "2026-10-02", objective: "Prepare event" }, headers: headers(),
    });
    expect(weekResponse.status()).toBe(201);
    const week = await weekResponse.json();
    const taskResponse = await page.request.post("/api/v1/tasks", {
      data: { project_id: project.id, project_week_id: week.id, title: "Survey", estimated_effort_hours: 4, required_skill_labels: [] }, headers: headers(),
    });
    expect(taskResponse.status()).toBe(201);
    await page.reload();
    await page.getByRole("button", { name, exact: false }).click();
    await page.getByRole("tab", { name: locale === "vi" ? "Báo cáo" : "Reports", exact: true }).click();
    await page.getByRole("button", { name: locale === "vi" ? "Tạo báo cáo" : "Create report", exact: true }).click();
    const zone = page.getByLabel(locale === "vi" ? "Múi giờ báo cáo" : "Reporting timezone", { exact: true });
    const defaults = await (await page.request.get(`/api/v1/reports/defaults?project_id=${project.id}`)).json();
    await expect(zone).toHaveValue(defaults.timezone);
    await zone.fill("Asia/Ho_Chi_Minh");
    await page.getByRole("button", { name: locale === "vi" ? "Tạo báo cáo số liệu" : "Generate report", exact: true }).click();
    const detail = page.getByRole("article", { name: locale === "vi" ? "Chi tiết báo cáo" : "Report detail" });
    await expect(detail).toBeVisible();
    await expect(detail.getByText("Asia/Ho_Chi_Minh", { exact: false })).toBeVisible();
    await expect(detail.getByText(locale === "vi" ? "Chưa xác định" : "Unknown", { exact: true })).toBeVisible();
    await expect(detail.getByText(locale === "vi" ? "Nhận xét AI chưa khả dụng. Bạn vẫn có thể sử dụng số liệu." : "AI commentary unavailable. Your metrics remain available.")).toBeVisible();
    const reports = await (await page.request.get(`/api/v1/reports?project_id=${project.id}`)).json();
    const reportId = reports.items[0].id;
    const before = await (await page.request.get(`/api/v1/reports/${reportId}`)).json();
    const task = await taskResponse.json();
    const edited = await page.request.patch(`/api/v1/tasks/${task.id}`, { data: { due_date: "2026-10-01" }, headers: { ...headers(), "If-Match": `"${task.version}"` } });
    expect(edited.status()).toBe(200);
    const after = await (await page.request.get(`/api/v1/reports/${reportId}`)).json();
    expect(after.snapshot).toEqual(before.snapshot);
    for (const width of [360, 1280]) {
      await page.setViewportSize({ width, height: 900 });
      await expect(detail).toBeVisible();
      const dimensions = await detail.evaluate(element => ({ width: element.clientWidth, scroll: element.scrollWidth }));
      expect(dimensions.scroll).toBeLessThanOrEqual(dimensions.width + 1);
    }
    await page.reload();
    await page.getByRole("button", { name, exact: false }).click();
    await page.getByRole("tab", { name: locale === "vi" ? "Báo cáo" : "Reports", exact: true }).click();
    await expect(page.getByRole("article", { name: locale === "vi" ? "Chi tiết báo cáo" : "Report detail" })).toBeVisible();
  });
}
