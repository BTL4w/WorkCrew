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
    await page.getByRole("checkbox", {name:locale === "vi" ? "Yêu cầu diễn giải bằng AI" : "Request an AI narrative"}).uncheck();
    await page.getByRole("button", { name: locale === "vi" ? "Tạo báo cáo số liệu" : "Generate report", exact: true }).click();
    const detail = page.getByRole("article", { name: locale === "vi" ? "Chi tiết báo cáo" : "Report detail" });
    await expect(detail).toBeVisible();
    await expect(detail.getByText("Asia/Ho_Chi_Minh", { exact: false }).first()).toBeVisible();
    await expect(detail.getByText(locale === "vi" ? "Chưa xác định" : "Unknown", { exact: true }).first()).toBeVisible();
    await expect(detail.getByText(locale === "vi" ? "Chưa yêu cầu diễn giải AI. Số liệu đã sẵn sàng." : "AI narrative was not requested. Metrics are ready.")).toBeVisible();
    const reports = await (await page.request.get(`/api/v1/reports?project_id=${project.id}`)).json();
    const reportId = reports.items[0].id;
    const before = await (await page.request.get(`/api/v1/reports/${reportId}`)).json();
    await detail.getByRole("button", {name: locale === "vi" ? "Xuất bản chỉ số liệu" : "Publish metrics only", exact:true}).click();
    await expect(detail.getByText(locale === "vi" ? "Bản xuất bản hiện tại" : "Current publication", {exact:true})).toBeVisible();
    const published = await (await page.request.get(`/api/v1/reports/${reportId}`)).json();
    expect(published.report.version).toBe(before.report.version + 1);
    expect(published.publications).toHaveLength(1);
    expect(published.publications[0].snapshot_hash).toBe(before.snapshot.snapshot_hash);
    expect(published.publications[0].report_version_id).toBe(before.selected_version.id);
    await detail.getByRole("button", {name:locale === "vi" ? "Xem bản đã xuất bản" : "View published version"}).click();
    const release = detail.getByRole("region", {name:locale === "vi" ? "Số liệu đã xuất bản" : "Published metrics", exact:true});
    await expect(release).toBeVisible();
    await release.getByText(locale === "vi" ? "Biên nhận số liệu" : "Metric snapshot receipt", {exact:true}).click();
    await expect(release.getByText(before.snapshot.snapshot_hash, {exact:true})).toBeVisible();
    await release.getByRole("button", {name:locale === "vi" ? "Đóng bản đã xuất bản" : "Close published version"}).click();
    const task = await taskResponse.json();
    const edited = await page.request.patch(`/api/v1/tasks/${task.id}`, { data: { due_date: "2026-10-01" }, headers: { ...headers(), "If-Match": `"${task.version}"` } });
    expect(edited.status()).toBe(200);
    const after = await (await page.request.get(`/api/v1/reports/${reportId}`)).json();
    expect(after.snapshot).toEqual(before.snapshot);
    expect(after.publications).toEqual(published.publications);
    expect(after.report.current_publication_id).toBe(published.report.current_publication_id);
    const sourceBefore = await (await page.request.get(`/api/v1/reports/${reportId}/sources`)).json();
    expect(sourceBefore.items.find((item: {source:{resource_id:string}}) => item.source.resource_id === task.id).freshness).toBe("UPDATED");
    await page.getByRole("button", {name: locale === "vi" ? "Tạo báo cáo" : "Create report", exact: true}).click();
    await page.getByLabel(locale === "vi" ? "Loại báo cáo" : "Report type", {exact:true}).selectOption("WEEKLY");
    await page.getByLabel(locale === "vi" ? "Ngày báo cáo" : "Reporting date", {exact:true}).fill("2026-09-28");
    await page.getByRole("checkbox", {name:locale === "vi" ? "Yêu cầu diễn giải bằng AI" : "Request an AI narrative"}).uncheck();
    await page.getByRole("button", {name:locale === "vi" ? "Tạo báo cáo số liệu" : "Generate report", exact:true}).click();
    await expect(detail.getByRole("heading", {name:/2026-09-28/})).toBeVisible();
    const weeklyReports = await (await page.request.get(`/api/v1/reports?project_id=${project.id}`)).json();
    const weekly = await (await page.request.get(`/api/v1/reports/${weeklyReports.items[0].id}`)).json();
    expect(weekly.report.kind).toBe("WEEKLY");
    expect(weekly.snapshot.metrics["tasks.status.total_count"].value).toBe("1");
    expect(weekly.snapshot.period.local_end).toBe("2026-10-05");
    expect(weekly.snapshot.metrics[`weekly.${week.id}.current.total_effort_hours`].value).toBe("4");
    await expect(detail.getByText(locale === "vi" ? "Kế hoạch và thực tế từng tuần" : "Weekly planned versus actual", {exact:true})).toBeVisible();
    await detail.getByRole("button", {name:locale === "vi" ? "Xuất bản chỉ số liệu" : "Publish metrics only", exact:true}).click();
    await expect(detail.getByText(locale === "vi" ? "Bản xuất bản hiện tại" : "Current publication", {exact:true})).toBeVisible();
    await detail.getByRole("button", {name:locale === "vi" ? "Xem bản đã xuất bản" : "View published version"}).click();
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
    await expect(page.getByText(locale === "vi" ? "Bản xuất bản hiện tại" : "Current publication", {exact:true})).toBeVisible();
  });
}
