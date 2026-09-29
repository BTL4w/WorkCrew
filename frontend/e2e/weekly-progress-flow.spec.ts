import { expect, test } from "@playwright/test";

 test("Manager sees immutable original and current plan with explicit unknown actuals", async ({ page }) => {
  await page.goto("/login");
  await page.getByLabel("Email").fill("manager@example.test");
  await page.getByLabel("Mật khẩu").fill("WorkDemo123!");
  await page.getByRole("button", { name: "Đăng nhập" }).click();
  await expect(page.getByRole("button", { name: "Đăng xuất" })).toBeVisible();
  const name = `Weekly progress ${Date.now()}`;
  const headers = () => ({"Idempotency-Key": crypto.randomUUID()});
  const projectResponse = await page.request.post("/api/v1/projects", {data: {name}, headers: headers()});
  expect(projectResponse.status()).toBe(201);
  const project = await projectResponse.json();
  const weekResponse = await page.request.post(`/api/v1/projects/${project.id}/weeks`, {data: {week_number: 1, start_date: "2026-09-28", end_date: "2026-10-02", objective: "Deliver"}, headers: headers()});
  expect(weekResponse.status()).toBe(201);
  const week = await weekResponse.json();
  let first;
  for (const [title, effort] of [["Small task", 2], ["Large task", 6]] as const) {
    const response = await page.request.post("/api/v1/tasks", {data: {project_id: project.id, project_week_id: week.id, title, estimated_effort_hours: effort, required_skill_labels: []}, headers: headers()});
    expect(response.status()).toBe(201);
    if (!first) first = await response.json();
  }
  const original = (await (await page.request.get(`/api/v1/projects/${project.id}/weeks/${week.id}/progress`)).json()).original;
  const edited = await page.request.patch(`/api/v1/tasks/${first.id}`, {data: {estimated_effort_hours: 4}, headers: {...headers(), "If-Match": `"${first.version}"`}});
  expect(edited.status()).toBe(200);
  const data = await (await page.request.get(`/api/v1/projects/${project.id}/weeks/${week.id}/progress`)).json();
  expect(data.original.baseline).toEqual(original.baseline);
  expect(data.current_plan.total_effort_hours).toBe("10");
  expect(data.current_plan.reported_percent).toBeNull();
  await page.reload();
  await page.getByRole("button", {name, exact: false}).click();
  await page.getByRole("tab", {name: "Kế hoạch"}).click();
  await expect(page.getByRole("heading", {name: "Tiến độ tuần"})).toBeVisible();
  await expect(page.getByText("Khối lượng có báo cáo: 0 / 10 giờ dự kiến")).toBeVisible();
  await expect(page.getByText("Task thêm: 2", {exact: false})).toBeVisible();
  await expect(page.getByText("Tiến độ phần đã báo cáo: Chưa xác định")).toHaveCount(2);
 });
