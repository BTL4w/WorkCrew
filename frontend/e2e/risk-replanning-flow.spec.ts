import { execFileSync } from "node:child_process";
import { resolve } from "node:path";
import { expect, test } from "@playwright/test";

test("A confirmed daily report can lead to a weekly risk proposal whose rejection changes no work", async ({
  page,
}) => {
  const backend = resolve(process.cwd(), "../backend");
  const env = {
    ...process.env,
    PYTHONPATH: backend,
    APP_DATABASE_URL:
      "postgresql+psycopg://work_management:work_management@localhost:5432/work_management_e2e",
  };
  const taskId = execFileSync(
    "uv",
    [
      "run",
      "--directory",
      backend,
      "python",
      resolve(process.cwd(), "e2e/fixtures/daily-update-seed.py"),
    ],
    { env },
  )
    .toString()
    .trim();
  async function login(email: string) {
    await page.goto("/login");
    await page.getByLabel("Email").fill(email);
    await page.getByLabel("Mật khẩu").fill("WorkDemo123!");
    await page.getByRole("button", { name: "Đăng nhập" }).click();
    await expect(page.getByRole("button", { name: "Đăng xuất" })).toBeVisible();
  }
  await login("manager@example.test");
  const originalTask = await (
    await page.request.get(`/api/v1/tasks/${taskId}`)
  ).json();
  const weeks = [];
  for (let index = 0; index < 2; index++) {
    const response = await page.request.post(
      `/api/v1/projects/${originalTask.project_id}/weeks`,
      {
        headers: { "Idempotency-Key": crypto.randomUUID() },
        data: {
          week_number: index + 1,
          start_date: index === 0 ? "2026-10-05" : "2026-10-12",
          end_date: index === 0 ? "2026-10-11" : "2026-10-18",
          objective: "Delivery review",
        },
      },
    );
    expect(response.status(), await response.text()).toBe(201);
    weeks.push(await response.json());
  }
  const update = await page.request.patch(`/api/v1/tasks/${taskId}`, {
    headers: {
      "Idempotency-Key": crypto.randomUUID(),
      "If-Match": `"${originalTask.version}"`,
    },
    data: {
      project_week_id: weeks[0].id,
      estimated_effort_hours: 8,
      due_date: "2026-10-11",
    },
  });
  expect(update.status(), await update.text()).toBe(200);
  await page.getByRole("button", { name: "Đăng xuất" }).click();
  await login("employee@example.test");
  const owned = await (
    await page.request.get(`/api/v1/tasks/${taskId}`)
  ).json();
  const create = await page.request.post("/api/v1/daily-updates/drafts", {
    headers: { "Idempotency-Key": crypto.randomUUID() },
    data: {
      items: [
        {
          task_id: taskId,
          expected_task_version: owned.version,
          expected_progress_version: 0,
          reporting_date: new Date().toLocaleDateString("en-CA", {
            timeZone: "Asia/Ho_Chi_Minh",
          }),
          reported_percent: "40",
          remaining_hours: "5",
          spent_hours: "3",
          done_text: "Completed the first review",
          next_steps: "Finish delivery review",
        },
      ],
    },
  });
  expect(create.status(), await create.text()).toBe(201);
  const draft = await create.json();
  const confirmed = await page.request.post(
    `/api/v1/daily-updates/${draft.id}/confirm`,
    {
      headers: { "Idempotency-Key": crypto.randomUUID() },
      data: { draft_id: draft.id, expected_draft_version: draft.version },
    },
  );
  expect(confirmed.status(), await confirmed.text()).toBe(201);
  await page.getByRole("button", { name: "Đăng xuất" }).click();
  await login("manager@example.test");
  execFileSync(
    "uv",
    [
      "run",
      "--directory",
      backend,
      "python",
      resolve(process.cwd(), "e2e/fixtures/risk-seed.py"),
      taskId,
    ],
    { env },
  );
  const before = await (
    await page.request.get(`/api/v1/tasks/${taskId}`)
  ).json();
  const baseline = await (
    await page.request.get(
      `/api/v1/projects/${owned.project_id}/weeks/${weeks[0].id}/progress`,
    )
  ).json();
  await page
    .getByRole("navigation", { name: "Điều hướng chính" })
    .getByRole("button", { name: "Cuộc trò chuyện mới" })
    .click();
  await page
    .getByLabel("Nhắn cho Trợ lý AI")
    .fill(`Điều chỉnh kế hoạch tuần do rủi ro công việc ${taskId}`);
  await page.getByRole("button", { name: "Gửi" }).click();
  const card = page.getByRole("region", {
    name: "Điều chỉnh kế hoạch tuần",
    exact: true,
  });
  await expect(card).toBeVisible();
  await expect(card.getByText("Tuần 1", { exact: true })).toBeVisible();
  await expect(card.getByText("Tuần 2", { exact: true })).toBeVisible();
  await expect(
    card.getByRole("button", { name: "Phê duyệt điều chỉnh", exact: true }),
  ).toBeEnabled();
  expect(
    await (await page.request.get(`/api/v1/tasks/${taskId}`)).json(),
  ).toEqual(before);
  await card.screenshot({ path: "/tmp/task12-risk-plan-card.png" });
  await card.getByRole("button", { name: "Từ chối", exact: true }).click();
  await expect(
    page.getByText("Đã từ chối · v1", { exact: true }),
  ).toBeVisible();
  await expect(
    card.getByRole("button", { name: "Phê duyệt điều chỉnh", exact: true }),
  ).toHaveCount(0);
  expect(
    await (await page.request.get(`/api/v1/tasks/${taskId}`)).json(),
  ).toEqual(before);
  const after = await (
    await page.request.get(
      `/api/v1/projects/${owned.project_id}/weeks/${weeks[0].id}/progress`,
    )
  ).json();
  expect(after.original.baseline).toEqual(baseline.original.baseline);
  const { evaluated_at: beforeAt, ...beforeMetrics } = baseline.current_plan;
  const { evaluated_at: afterAt, ...afterMetrics } = after.current_plan;
  expect(beforeAt).toBeTruthy();
  expect(afterAt).toBeTruthy();
  expect(afterMetrics).toEqual(beforeMetrics);
});
