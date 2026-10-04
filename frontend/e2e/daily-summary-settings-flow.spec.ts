import { expect, test } from "@playwright/test";

test("Manager confirms, edits and pauses a schedule without changing the current window", async ({
  page,
}) => {
  await page.goto("/login");
  await page.getByLabel("Email").fill("manager@example.test");
  await page.getByLabel("Mật khẩu").fill("WorkDemo123!");
  await page.getByRole("button", { name: "Đăng nhập" }).click();
  await expect(page.getByRole("button", { name: "Đăng xuất" })).toBeVisible();
  const name = `Schedule ${crypto.randomUUID()}`;
  const created = await page.request.post("/api/v1/projects", {
    headers: { "Idempotency-Key": crypto.randomUUID() },
    data: { name, description: "Daily schedule browser test" },
  });
  expect(created.status(), await created.text()).toBe(201);
  const project = await created.json();
  await page
    .getByRole("navigation", { name: "Điều hướng chính" })
    .getByRole("button", { name: "Projects", exact: true })
    .click();
  await page.reload();
  await page.getByRole("button", { name: new RegExp(name) }).click();
  await page.getByRole("tab", { name: "Tổng hợp hằng ngày" }).click();
  const settings = page.getByRole("region", {
    name: "Lịch tổng hợp hằng ngày",
  });
  await expect(settings).toBeVisible();
  await settings.getByLabel("Múi giờ IANA").fill("Asia/Ho_Chi_Minh");
  await settings.getByLabel("Giờ chốt báo cáo").fill("17:00");
  await settings
    .getByText("Người nhận", { exact: true })
    .locator("..")
    .getByRole("checkbox")
    .first()
    .check();
  await settings.getByRole("button", { name: "Xem trước lịch" }).click();
  await expect(
    settings.getByRole("heading", { name: "Xác nhận lịch tổng hợp" }),
  ).toBeVisible();
  const before = await (
    await page.request.get(
      `/api/v1/automations/daily-summaries?project_id=${project.id}`,
    )
  ).json();
  expect(before.schedule).toBeNull();
  await settings.getByRole("button", { name: "Xác nhận lưu lịch" }).click();
  await expect(settings.getByText(/Không có người cần báo cáo/)).toBeVisible();
  const initial = await (
    await page.request.get(
      `/api/v1/automations/daily-summaries?project_id=${project.id}`,
    )
  ).json();
  expect(initial.schedule.version).toBe(1);
  await settings.getByLabel("Giờ chốt báo cáo").fill("18:00");
  await settings.getByRole("button", { name: "Xem trước lịch" }).click();
  await settings.getByRole("button", { name: "Xác nhận lưu lịch" }).click();
  await expect(settings.getByLabel("Giờ chốt báo cáo")).toHaveValue("18:00");
  const edited = await (
    await page.request.get(
      `/api/v1/automations/daily-summaries?project_id=${project.id}`,
    )
  ).json();
  expect(edited.schedule.version).toBe(2);
  expect(edited.window.id).toBe(initial.window.id);
  expect(edited.window.cutoff_at).toBe(initial.window.cutoff_at);
  await settings.screenshot({ path: "/tmp/task13-settings.png" });
  await settings.getByRole("button", { name: "Tạm dừng lịch" }).click();
  await expect(
    settings.getByText("Lịch đang tạm dừng.", { exact: true }),
  ).toBeVisible();
  await settings.getByRole("button", { name: "Tiếp tục lịch" }).click();
  await expect(
    settings.getByRole("button", { name: "Tạm dừng lịch" }),
  ).toBeVisible();
});
