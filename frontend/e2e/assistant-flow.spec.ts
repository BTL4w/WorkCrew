import { expect, test, type Page } from "@playwright/test";

const password = "WorkDemo123!";

async function signIn(page: Page, email: string) {
  await page.goto("/login");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Mật khẩu").fill(password);
  await page.getByRole("button", { name: "Đăng nhập" }).click();
  await expect(page.getByRole("button", { name: "Đăng xuất" })).toBeVisible();
}

async function signOut(page: Page) {
  await page.getByRole("button", { name: "Đăng xuất" }).click();
  await expect(page).toHaveURL(/\/login$/);
}

async function openNewConversation(page: Page) {
  await page
    .getByRole("navigation", { name: "Điều hướng chính" })
    .getByRole("button", { name: "Cuộc trò chuyện mới" })
    .click();
  await expect(page.getByRole("heading", { name: "Trợ lý AI" })).toBeVisible();
}

async function send(page: Page, message: string) {
  const composer = page.getByLabel("Nhắn cho Trợ lý AI");
  await composer.fill(message);
  await page.getByRole("button", { name: "Gửi" }).click();
  await expect
    .poll(
      async () => {
        const conversationId = new URL(page.url()).searchParams.get("conversation");
        if (!conversationId) return false;
        const response = await page.request.get(`/api/v1/ai/conversations/${conversationId}`);
        if (!response.ok()) return false;
        const snapshot = (await response.json()) as {
          conversation?: { last_event_sequence?: number };
          messages?: Array<{ content_blocks?: Array<{ kind?: string }> }>;
        };
        return Boolean(
          snapshot.conversation?.last_event_sequence &&
            snapshot.messages?.some((item) =>
              item.content_blocks?.some((block) => block.kind === "activity"),
            ),
        );
      },
      { message: "persisted assistant activity event", timeout: 20_000 },
    )
    .toBe(true);
  await expect(page.locator(".assistant-activity").first()).toBeVisible();
}

async function getItems<T>(page: Page, path: string): Promise<T[]> {
  const response = await page.request.get(path);
  expect(response.ok(), `GET ${path}`).toBe(true);
  return ((await response.json()) as { items: T[] }).items;
}

test("Assistant enforces Employee scope and completes one approval-gated Manager plan", async ({
  page,
}) => {
  await signIn(page, "employee@example.test");
  await openNewConversation(page);
  await send(page, "Công việc hiện tại và task tiếp theo của tôi là gì?");
  await expect(page.getByRole("heading", { name: "Bằng chứng công việc" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Phê duyệt kế hoạch" })).toHaveCount(0);

  await openNewConversation(page);
  await send(page, "Hãy lập một project plan và tự phê duyệt cho tôi");
  await expect(page.getByRole("heading", { name: "Khả năng chưa khả dụng" })).toBeVisible();
  await expect(page.locator(".assistant-planning-card")).toHaveCount(0);
  await signOut(page);

  await signIn(page, "manager@example.test");
  await openNewConversation(page);
  await send(page, "Lập kế hoạch ra mắt sản phẩm trong quý tới");

  await expect(page.getByText("Proposal v1", { exact: true })).toBeVisible();
  await expect(page.getByText("Chờ bạn phê duyệt")).toBeVisible();

  await page.getByRole("button", { name: "Nhờ AI chỉnh" }).click();
  await page.getByLabel("Yêu cầu AI chỉnh proposal").fill(
    "Dời mốc cuối một tuần và bổ sung công việc xác nhận nhà cung cấp.",
  );
  await page.getByRole("button", { name: "Gửi yêu cầu chỉnh" }).click();

  await expect(page.getByText("Proposal v2", { exact: true })).toBeVisible();
  await expect(page.getByText("Version chỉ đọc").first()).toBeVisible();
  await page.getByRole("button", { name: "Phê duyệt kế hoạch" }).click();
  await expect(page.getByRole("heading", { name: "Kết quả quyết định" })).toBeVisible();

  await page.getByRole("button", { name: "Projects" }).click();
  await expect(page.getByRole("button", { name: "Proposed project" })).toHaveCount(1);

  const projects = await getItems<{ id: string; name: string }>(page, "/api/v1/projects");
  const project = projects.find((item) => item.name === "Proposed project");
  expect(project).toBeDefined();
  const projectId = project!.id;
  const goals = await getItems<{ project_id: string; title: string }>(
    page,
    `/api/v1/goals?project_id=${projectId}`,
  );
  const milestones = await getItems<{ project_id: string; id: string }>(
    page,
    `/api/v1/milestones?project_id=${projectId}`,
  );
  const weeks = await getItems<{ project_id: string; id: string }>(
    page,
    `/api/v1/projects/${projectId}/weeks`,
  );
  const tasks = await getItems<{
    id: string;
    title: string;
    project_id: string;
    project_week_id: string | null;
    milestone_id: string | null;
    assignee: null | { membership_id: string };
  }>(page, `/api/v1/tasks?project_id=${projectId}`);
  const dependencies = await getItems<{
    predecessor_task_id: string;
    successor_task_id: string;
  }>(page, `/api/v1/task-dependencies?project_id=${projectId}`);
  const criteria = (
    await Promise.all(
      tasks.map((task) =>
        getItems<{ task_id: string }>(page, `/api/v1/acceptance-criteria?task_id=${task.id}`),
      ),
    )
  ).flat();

  expect(goals).toHaveLength(1);
  expect(goals[0]).toMatchObject({ project_id: projectId, title: "Proposed goal" });
  expect(milestones).toHaveLength(1);
  expect(weeks).toHaveLength(1);
  expect(tasks).toHaveLength(2);
  expect(tasks.every((task) => task.assignee === null)).toBe(true);
  expect(tasks.every((task) => task.project_week_id === weeks[0].id)).toBe(true);
  expect(tasks.every((task) => task.milestone_id === milestones[0].id)).toBe(true);
  expect(dependencies).toHaveLength(1);
  const prepareTask = tasks.find((task) => task.title === "Prepare launch package");
  const verifyTask = tasks.find((task) => task.title === "Verify launch readiness");
  expect(prepareTask).toBeDefined();
  expect(verifyTask).toBeDefined();
  expect(dependencies[0]).toMatchObject({
    predecessor_task_id: prepareTask!.id,
    successor_task_id: verifyTask!.id,
  });
  expect(criteria).toHaveLength(2);
  expect(criteria.map((item) => item.task_id).sort()).toEqual(tasks.map((item) => item.id).sort());

  await page.locator(".assistant-conversation-items button").first().click();
  await page.reload();
  await expect(page.getByText("Proposal v2", { exact: true })).toHaveCount(1);
  await expect(page.getByRole("heading", { name: "Kết quả quyết định" })).toHaveCount(1);

  await openNewConversation(page);
  await send(page, "Lập một kế hoạch khác để tôi cân nhắc");
  await expect(page.getByText("Proposal v1", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Từ chối" }).click();
  await expect(page.getByRole("heading", { name: "Kết quả quyết định" })).toBeVisible();
  const projectsAfterRejection = await getItems<{ name: string }>(page, "/api/v1/projects");
  expect(projectsAfterRejection.filter((item) => item.name === "Proposed project")).toHaveLength(1);
});

test("combined Project and team request checkpoints approval before recommending a team", async ({ page }) => {
  await signIn(page, "manager@example.test");
  await page.getByRole("button", { name: "Con người & kỹ năng" }).click();
  await expect(page.getByRole("heading", { name: "Bùi Thảo Nguyên", level: 3 })).toBeVisible();
  if (await page.getByRole("button", { name: "Sửa Manual Testing của Bùi Thảo Nguyên" }).count() === 0) {
    await page.getByRole("button", { name: "Thêm skill" }).click();
    const skillDialog = page.getByRole("dialog", { name: "Thêm kỹ năng đã xác minh" });
    await skillDialog.getByLabel("Thành viên").selectOption({ label: "Bùi Thảo Nguyên" });
    await skillDialog.getByLabel("Skill").selectOption({ label: "Manual Testing" });
    await skillDialog.getByLabel("Mức độ").selectOption("5");
    await skillDialog.getByLabel("Evidence").fill("E2E verified test experience");
    await skillDialog.getByRole("button", { name: "Lưu skill" }).click();
  }
  await expect(page.getByRole("button", { name: "Sửa Manual Testing của Bùi Thảo Nguyên" })).toBeVisible();
  await openNewConversation(page);
  await send(page, "Lập kế hoạch dự án và đề xuất đội ngũ thực hiện");

  await expect(page.getByText("Proposal v1", { exact: true })).toBeVisible();
  await expect(page.locator(".assistant-team-recommendation")).toHaveCount(0);
  await page.getByRole("button", { name: "Phê duyệt kế hoạch" }).click();
  await expect(page.getByRole("heading", { name: "Kết quả quyết định" })).toBeVisible();
  await expect(page.locator(".assistant-team-recommendation")).toBeVisible();

  await page.getByRole("button", { name: "Yêu cầu chỉnh đội" }).click();
  await expect(page.getByText("Đang chỉnh đề xuất đội ngũ từ v1")).toBeVisible();
  const selectedNam = await page.locator(".assistant-team-recommendation").getByText("Đỗ Ngọc Nam").count() > 0;
  const oldName = selectedNam ? "Đỗ Ngọc Nam" : "Bùi Thảo Nguyên";
  const newName = selectedNam ? "Bùi Thảo Nguyên" : "Đỗ Ngọc Nam";
  await send(page, `Thay ${oldName} bằng ${newName} vì cần điều chỉnh đội cho kiểm thử.`);
  await expect(page.getByRole("heading", { name: "Phiên bản 2" })).toBeVisible();
  await expect(page.getByText("Đề xuất v1 chỉ đọc vì đã có phiên bản mới hơn.")).toBeVisible();
  await page.getByRole("button", { name: "Phê duyệt đội ngũ" }).click();
  await page.getByRole("button", { name: "Xác nhận" }).click();
  await expect(page.locator(".assistant-team-recommendation").last().getByText("Đã duyệt")).toBeVisible();

  const projects = await getItems<{ id: string; name: string }>(page, "/api/v1/projects");
  const project = projects.find((item) => item.name === "Proposed project");
  expect(project).toBeDefined();
  const tasks = await getItems<{ assignee: unknown }>(page, `/api/v1/tasks?project_id=${project!.id}`);
  expect(tasks.length).toBeGreaterThan(0);
  expect(tasks.every((task) => task.assignee === null)).toBe(true);
});
