import { expect, test } from "@playwright/test";

for (const locale of ["vi", "en"] as const) {
  test(`scheduled digest produces a separate reviewed report (${locale})`, async ({ page }) => {
    await page.goto("/login");
    await page.getByLabel("Email").fill("manager@example.test");
    await page.getByLabel("Mật khẩu").fill("WorkDemo123!");
    await page.getByRole("button", { name: "Đăng nhập" }).click();
    await expect(page.getByRole("button", { name: "Đăng xuất" })).toBeVisible();
    if (locale === "en") await page.getByRole("button", { name: "en", exact: true }).click();
    const name = `Scheduled report ${locale} ${crypto.randomUUID()}`;
    const response = await page.request.post("/api/v1/projects", {
      headers: { "Idempotency-Key": crypto.randomUUID() }, data: { name },
    });
    expect(response.status()).toBe(201);
    const project = await response.json();
    await page.reload();
    await page.getByRole("button", { name: new RegExp(name) }).click();
    const automationTab = locale === "vi" ? "Tổng hợp hằng ngày" : "Daily summaries";
    await page.getByRole("tab", { name: automationTab, exact: true }).click();
    await page.getByLabel(locale === "vi" ? "Múi giờ IANA" : "IANA timezone", { exact: true }).fill("UTC");
    await page.getByLabel(locale === "vi" ? "Giờ chốt báo cáo" : "Reporting cutoff", { exact: true }).fill("00:00");
    await page.getByRole("checkbox", { name: locale === "vi" ? "Thứ Bảy" : "Saturday", exact: true }).check();
    await page.getByRole("checkbox", { name: locale === "vi" ? "Chủ Nhật" : "Sunday", exact: true }).check();
    await page.getByRole("checkbox", { name: "Demo Manager", exact: true }).check();
    await page.getByRole("checkbox", { name: locale === "vi" ? "Tạo bản nháp diễn giải AI" : "Create an AI narrative draft", exact: true }).check();
    await page.getByRole("button", { name: locale === "vi" ? "Xem trước lịch" : "Preview schedule", exact: true }).click();
    await page.getByRole("button", { name: locale === "vi" ? "Xác nhận lưu lịch" : "Confirm schedule", exact: true }).click();
    const feed = async () => (await (await page.request.get("/api/v1/automations/daily-summaries/deliveries")).json())
      .filter((d: { snapshot: { project_id: string } }) => d.snapshot.project_id === project.id);
    await expect.poll(async () => {
      const cards = await feed();
      return cards[0]?.report_link?.generation_state;
    }, { timeout: 60000 }).toBe(process.env.APP_AI_PROVIDER === "disabled" ? "AI_UNAVAILABLE" : "AWAITING_REVIEW");
    const original = await feed();
    expect(original).toHaveLength(1);
    await expect(page.getByRole("link", { name: locale === "vi" ? "Mở report" : "Open report" })).toBeVisible({ timeout: 30000 });
    await page.getByRole("link", { name: locale === "vi" ? "Mở report" : "Open report" }).click();
    await expect(page.getByText(locale === "vi" ? "Chỉ các mục được đưa vào tổng hợp; không phải tổng toàn Project. Mở report mới để chụp đầy đủ." : "Included digest items only; these are not Project totals. Create a fresh report for a full capture.").first()).toBeVisible();
    const reportId = original[0].report_link.report_id;
    const before = await (await page.request.get(`/api/v1/reports/${reportId}`)).json();
    expect(before.report.origin).toBe("DAILY_SUMMARY");
    expect(before.report.summary_id).toBe(original[0].snapshot.id);
    expect(before.publications).toHaveLength(0);
    if (process.env.APP_AI_PROVIDER !== "disabled") {
      await page.getByRole("button", { name: locale === "vi" ? "Duyệt và xuất bản" : "Review and publish" }).click();
      await expect.poll(async () => (await feed())[0].report_link.publication_id).not.toBeNull();
    }
    const after = await feed();
    expect(after).toHaveLength(1);
    expect(after[0].snapshot).toEqual(original[0].snapshot);
    const finalReport = await (await page.request.get(`/api/v1/reports/${reportId}`)).json();
    expect(finalReport.snapshot).toEqual(before.snapshot);
  });
}
