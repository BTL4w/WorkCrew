import { expect, test, type Page } from "@playwright/test";

async function signIn(page: Page, email: string) {
  await page.goto("/login");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Mật khẩu").fill("WorkDemo123!");
  await page.getByRole("button", { name: "Đăng nhập" }).click();
  await expect(page.getByRole("button", { name: "Đăng xuất" })).toBeVisible();
}

test("Team approval leaves Tasks unassigned until an explicit Manager assignment", async ({ page }) => {
  const projectName = `Phase 3 team ${Date.now()}`;
  const taskName = `Phase 3 task ${Date.now()}`;
  await signIn(page, "manager@example.test");
  await page.getByRole("button", { name: "Projects", exact: true }).click();
  await page.getByRole("button", { name: "Tạo project" }).click();
  await page.getByLabel("Tên project").fill(projectName);
  await page.getByLabel("Mô tả project").fill("Project Team browser acceptance");
  await page.getByRole("button", { name: "Lưu project" }).click();
  await expect(page.getByRole("heading", { name: projectName })).toBeVisible();

  await page.getByRole("tab", { name: "Kế hoạch" }).click();
  await page.getByRole("button", { name: "Thêm tuần" }).click();
  await page.getByLabel("Ngày bắt đầu").fill("2026-10-05");
  await page.getByLabel("Ngày kết thúc").fill("2026-10-11");
  await page.getByLabel("Mục tiêu tuần").fill("Complete Phase 3 browser test");
  await page.getByRole("button", { name: "Lưu tuần" }).click();
  await page.getByRole("tab", { name: "Tasks" }).click();
  await page.getByRole("button", { name: "Tạo task" }).click();
  await page.getByLabel("Tiêu đề task").fill(taskName);
  await page.getByLabel("Tuần dự án").selectOption({ label: "Tuần 1" });
  await page.getByLabel("Kỹ năng cần thiết, mỗi dòng một mục").fill("Manual Testing");
  await page.getByLabel("Số giờ công ước tính").fill("8");
  await page.getByRole("button", { name: "Lưu task" }).click();
  await expect(page.getByText("Chưa giao")).toBeVisible();
  await page.getByRole("button", { name: "← Quay lại" }).click();

  const projectResponse = await page.request.get("/api/v1/projects");
  expect(projectResponse.ok()).toBe(true);
  const project = ((await projectResponse.json()) as { items: Array<{ id: string; name: string }> }).items.find(
    (item) => item.name === projectName,
  );
  expect(project).toBeDefined();
  const projectId = project!.id;

  await page.getByRole("tab", { name: "Đội ngũ" }).click();
  await page.getByRole("button", { name: "Tạo yêu cầu từ task" }).click();
  await page.getByRole("button", { name: "Xác nhận yêu cầu" }).click();
  const createdRecommendation = page.waitForResponse((response) =>
    response.url().includes(`/api/v1/projects/${projectId}/team-recommendations`) && response.request().method() === "POST",
  );
  await page.getByRole("button", { name: "Tạo đề xuất đội ngũ" }).click();
  const recommendationId = ((await (await createdRecommendation).json()) as { recommendation_id: string }).recommendation_id;
  await expect(page.getByRole("heading", { name: "Phiên bản 1" })).toBeVisible();
  await page.getByText("Điểm số, minh chứng và khối lượng").click();
  await expect(page.getByText("Kỹ năng", { exact: true }).first()).toBeVisible();

  await page.getByRole("button", { name: "Đổi thành viên" }).click();
  await page.getByRole("button", { name: "Lưu thành phiên bản 2" }).click();
  await expect(page.getByRole("heading", { name: "Phiên bản 2" })).toBeVisible();
  await expect(page.getByText("Phiên bản 1 · chỉ đọc")).toBeVisible();

  const taskResponse = await page.request.get(`/api/v1/tasks?project_id=${projectId}`);
  expect(taskResponse.ok()).toBe(true);
  const task = ((await taskResponse.json()) as { items: Array<{ id: string; assignee: unknown }> }).items[0];
  expect(task.assignee).toBeNull();
  const staleApproval = await page.request.post(`/api/v1/recommendations/${recommendationId}/approve`, {
    headers: { "Content-Type": "application/json", "If-Match": '"1"', "Idempotency-Key": crypto.randomUUID() },
    data: { action: "approve", reason: null },
  });
  expect(staleApproval.status()).toBe(412);

  await page.getByRole("button", { name: "Phê duyệt đội ngũ" }).click();
  await page.getByRole("button", { name: "Xác nhận phê duyệt" }).click();
  await expect(page.getByRole("heading", { name: "Đội ngũ hiện tại" })).toBeVisible();
  const stillUnassigned = await page.request.get(`/api/v1/tasks/${task.id}`);
  expect(stillUnassigned.ok()).toBe(true);
  expect(((await stillUnassigned.json()) as { assignee: unknown }).assignee).toBeNull();

  await page.getByRole("tab", { name: "Tasks" }).click();
  await page.getByRole("button", { name: taskName }).click();
  await page.getByLabel("Giao task từ Project Team").getByRole("button", { name: "Giao task" }).click();
  const memberSelect = page.getByLabel("Thành viên dự án");
  await expect(memberSelect.locator("option")).toHaveCount(2);
  await memberSelect.selectOption({ index: 1 });
  const selectedMember = (await memberSelect.locator("option:checked").textContent())!.trim();
  await page.getByRole("button", { name: `Xác nhận giao cho ${selectedMember}` }).click();
  await expect(page.getByText(`Đã giao cho ${selectedMember}`)).toBeVisible();
  const assigned = await page.request.get(`/api/v1/tasks/${task.id}`);
  expect(assigned.ok()).toBe(true);
  expect(((await assigned.json()) as { assignee: unknown }).assignee).not.toBeNull();
  if (process.env.APP_AI_PROVIDER === "disabled") return;

  const chatTaskTitle = `Phase 3 chat assignment ${Date.now()}`;
  const chatTask = await page.request.post("/api/v1/tasks", {
    headers: { "Idempotency-Key": crypto.randomUUID() },
    data: {
      project_id: projectId,
      project_week_id: ((await assigned.json()) as { project_week_id: string }).project_week_id,
      title: chatTaskTitle,
      assignee_membership_id: null,
      required_skill_labels: ["Manual Testing"],
      estimated_effort_hours: 2,
      due_date: null,
    },
  });
  expect(chatTask.status()).toBe(201);
  const chatTaskId = ((await chatTask.json()) as { id: string }).id;
  await page.getByRole("navigation", { name: "Điều hướng chính" })
    .getByRole("button", { name: "Cuộc trò chuyện mới" }).click();
  await page.getByLabel("Nhắn cho Trợ lý AI").fill(`Giao ${chatTaskTitle} cho ${selectedMember}`);
  await page.getByRole("button", { name: "Gửi" }).click();
  await expect(page.getByRole("heading", { name: "Kết quả giao Task" })).toBeVisible();
  const chatAssigned = await page.request.get(`/api/v1/tasks/${chatTaskId}`);
  expect(chatAssigned.ok()).toBe(true);
  expect(((await chatAssigned.json()) as { assignee: unknown }).assignee).not.toBeNull();
});
