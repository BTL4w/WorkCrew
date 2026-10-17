import { expect, test, type Page } from "@playwright/test";

const password = "WorkDemo123!";
type Snapshot = {
  messages: Array<{
    content_blocks: Array<{
      kind: string;
      workflow_run_id?: string;
      draft_id?: string;
      draft_version?: number;
      proposal_id?: string;
      proposal_version?: number;
      approval_id?: string;
    }>;
  }>;
};
async function snapshot(page: Page): Promise<Snapshot> {
  const id = new URL(page.url()).searchParams.get("conversation");
  const response = await page.request.get(`/api/v1/ai/conversations/${id}`);
  expect(response.ok()).toBeTruthy();
  return response.json();
}
async function send(page: Page, message: string, locale: "vi" | "en") {
  await page
    .getByLabel(
      locale === "vi" ? "Nhắn cho Trợ lý AI" : "Message the AI Assistant",
    )
    .fill(message);
  await page
    .getByRole("button", {
      name: locale === "vi" ? "Gửi" : "Send",
      exact: true,
    })
    .click();
  await expect(page).toHaveURL(/[?&]conversation=[0-9a-f-]+(?:&|$)/);
}
async function latestDraft(page: Page, version: number) {
  let found: Snapshot["messages"][number]["content_blocks"][number] | undefined;
  await expect
    .poll(
      async () => {
        found = (await snapshot(page)).messages
          .flatMap((message) => message.content_blocks)
          .filter(
            (block) =>
              block.kind === "planning_draft" &&
              block.draft_version === version,
          )
          .at(-1);
        return Boolean(found);
      },
      { timeout: 30000 },
    )
    .toBe(true);
  return found!;
}
for (const locale of ["vi", "en"] as const) {
  test(`Progressive Planning ${locale}: missing goal, concrete draft, edits and exact confirmation`, async ({
    page,
  }) => {
    await page.goto("/login");
    await page.getByLabel("Email").fill("manager@example.test");
    await page.getByLabel("Mật khẩu").fill(password);
    await page.getByRole("button", { name: "Đăng nhập", exact: true }).click();
    await expect(page.getByRole("button", { name: "Đăng xuất" })).toBeVisible();
    if (locale === "en")
      await page.getByRole("button", { name: "en", exact: true }).click();
    const composer = page.getByLabel(
      locale === "vi" ? "Nhắn cho Trợ lý AI" : "Message the AI Assistant",
    );
    await expect(composer).toBeVisible();
    await send(
      page,
      locale === "vi" ? "Tạo Plan cho tôi" : "Create a plan for me",
      locale,
    );
    await expect(
      page.getByText(locale === "vi" ? /Thiếu mục tiêu/ : /Missing information/),
    ).toBeVisible();
    await expect(page.getByRole("heading", {
      name: locale === "vi" ? "Cần thêm thông tin" : "More information needed",
    })).toHaveCount(0);
    await expect(page.locator(".assistant-question")).toHaveCount(0);
    expect(
      (await snapshot(page)).messages
        .flatMap((message) => message.content_blocks)
        .filter((block) => ["planning_draft", "proposal", "planning_run"].includes(block.kind)),
    ).toHaveLength(0);
    await expect(page.locator(".assistant-planning-draft")).toHaveCount(0);
    await send(
      page,
      locale === "vi"
        ? "Tôi muốn ra mắt khóa học tiếng Anh online, dành cho người đi làm, mục tiêu 100 học viên."
        : "I want to launch an online English course for working adults, targeting 100 learners.",
      locale,
    );
    const initial = await latestDraft(page, 1);
    await expect(
      page.locator(".assistant-planning-draft").first(),
    ).toBeVisible();
    await expect(
      page.getByRole("button", {
        name: locale === "vi" ? "Phê duyệt kế hoạch" : "Approve plan",
        exact: true,
      }),
    ).toHaveCount(0);
    await send(
      page,
      locale === "vi"
        ? "Giữ 6 tuần, ngân sách 20 triệu"
        : "Keep six weeks, with a budget of VND 20 million",
      locale,
    );
    const second = await latestDraft(page, 2);
    expect(second.draft_id).toBe(initial.draft_id);
    await page.reload();
    const restored = await latestDraft(page, 2);
    expect(restored.draft_id).toBe(initial.draft_id);
    const current = page.locator(
      `.assistant-planning-draft[data-draft-id="${initial.draft_id}"][data-draft-version="2"]`,
    );
    await expect(current).toContainText("20,000,000");
    await send(
      page,
      locale === "vi"
        ? "Tạo kế hoạch khác về tuyển dụng"
        : "Create another plan for recruitment",
      locale,
    );
    let other:
      | Snapshot["messages"][number]["content_blocks"][number]
      | undefined;
    await expect
      .poll(
        async () => {
          other = (await snapshot(page)).messages
            .flatMap((message) => message.content_blocks)
            .filter(
              (block) =>
                block.kind === "planning_draft" &&
                block.draft_id !== initial.draft_id,
            )
            .at(-1);
          return Boolean(other);
        },
        { timeout: 30000 },
      )
      .toBe(true);
    const untouched = await page.request.get(
      `/api/v1/planning-drafts/${initial.draft_id}`,
    );
    expect((await untouched.json()).current_version).toBe(2);
    await expect(current).toContainText("20,000,000");
    const anchor = new Date(Date.now() + 7 * 86400000)
      .toISOString()
      .slice(0, 10);
    await current
      .getByLabel(locale === "vi" ? "Ngày bắt đầu" : "Start date")
      .fill(anchor);
    await current
      .getByRole("button", {
        name:
          locale === "vi" ? "Chuẩn bị tạo dự án" : "Prepare project creation",
        exact: true,
      })
      .click();
    let old: Snapshot["messages"][number]["content_blocks"][number] | undefined;
    await expect
      .poll(
        async () => {
          old = (await snapshot(page)).messages
            .flatMap((message) => message.content_blocks)
            .find(
              (block) =>
                block.kind === "proposal" && block.proposal_version === 1,
            );
          return Boolean(old?.approval_id);
        },
        { timeout: 30000 },
      )
      .toBe(true);
    expect(old!.workflow_run_id).toBe(initial.workflow_run_id);
    await send(
      page,
      locale === "vi"
        ? "Đổi ngân sách thành 10 triệu"
        : "Change the budget to VND 10 million",
      locale,
    );
    await latestDraft(page, 3);
    const stale = await page.request.post(
      `/api/v1/approvals/${old!.approval_id}/decision`,
      {
        headers: {
          "Idempotency-Key": `stale-${locale}-${Date.now()}`,
          "If-Match": '"1"',
        },
        data: { decision: "APPROVE", reason: null },
      },
    );
    expect([409, 422]).toContain(stale.status());
    const newest = page.locator(
      `.assistant-planning-draft[data-draft-id="${initial.draft_id}"][data-draft-version="3"]`,
    );
    await expect(newest).toContainText("10,000,000");
    const preserved = await page.request.get(
      `/api/v1/planning-drafts/${other!.draft_id}`,
    );
    expect((await preserved.json()).current_version).toBe(1);
    await newest
      .getByLabel(locale === "vi" ? "Ngày bắt đầu" : "Start date")
      .fill(anchor);
    await newest
      .getByRole("button", {
        name:
          locale === "vi" ? "Chuẩn bị tạo dự án" : "Prepare project creation",
        exact: true,
      })
      .click();
    await expect
      .poll(
        async () =>
          Boolean(
            (await snapshot(page)).messages
              .flatMap((message) => message.content_blocks)
              .find(
                (block) =>
                  block.kind === "proposal" && block.proposal_version === 2,
              ),
          ),
        { timeout: 30000 },
      )
      .toBe(true);
    await page
      .getByRole("button", {
        name: locale === "vi" ? "Phê duyệt kế hoạch" : "Approve plan",
        exact: true,
      })
      .last()
      .click();
    await expect(
      page.getByRole("heading", {
        name: locale === "vi" ? "Kết quả quyết định" : "Decision result",
      }),
    ).toBeVisible();
  });
}
