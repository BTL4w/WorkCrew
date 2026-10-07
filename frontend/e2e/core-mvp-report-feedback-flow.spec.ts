import { expect, test, type Page } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { resolve } from "node:path";
import vi from "../src/shared/i18n/messages/vi.json" with { type: "json" };
import en from "../src/shared/i18n/messages/en.json" with { type: "json" };

type Locale = "vi" | "en";
function label(locale: Locale, key: string): string {
  let value: unknown = locale === "vi" ? vi : en;
  for (const part of key.split(".")) value = (value as Record<string, unknown>)[part];
  if (typeof value !== "string") throw new Error(`Missing translation: ${key}`);
  return value;
}
async function login(page: Page, email: string, locale: Locale) {
  await page.goto("/login");
  await page.getByLabel("Email", { exact: true }).fill(email);
  await page.getByLabel(/^(Mật khẩu|Password)$/).fill("WorkDemo123!");
  await page.getByRole("button", { name: /^(Đăng nhập|Sign in)$/ }).click();
  await expect(page.getByRole("button", { name: /^(Đăng xuất|Sign out)$/ })).toBeVisible();
  await page.getByRole("button", { name: locale, exact: true }).click();
}
async function send(page: Page, locale: Locale, message: string) {
  await page.getByLabel(label(locale, "assistant.composer.label")).fill(message);
  await page.getByRole("button", { name: label(locale, "assistant.composer.send"), exact: true }).click();
}
async function logout(page: Page) {
  await page.getByRole("button", { name: /^(Đăng xuất|Sign out)$/ }).click();
}
const headers = () => ({ "Idempotency-Key": crypto.randomUUID() });

for (const locale of ["vi", "en"] as const) {
  test(`Core MVP project to feedback and Admin evaluation (${locale})`, async ({ page }) => {
    test.skip(process.env.APP_AI_PROVIDER === "disabled", "Primary spine requires mock AI; fallback runs separately");
    test.setTimeout(240_000);
    const l = (key: string) => label(locale, key);
    await login(page, "manager@example.test", locale);
    const before = (await (await page.request.get("/api/v1/projects?page_size=100")).json()).items;
    await page.getByRole("navigation").getByRole("button", { name: l("assistant.conversations.new"), exact: true }).click();
    await send(page, locale, locale === "vi" ? "Lập kế hoạch ra mắt sản phẩm trong quý tới" : "Create a project plan for a product launch next quarter");
    await expect(page.locator(".assistant-question, .assistant-planning-card").first()).toBeVisible();
    if (await page.getByRole("heading", { name: l("assistant.question.title") }).isVisible()) {
      await send(page, locale, locale === "vi" ? "Ngân sách đã được duyệt, phạm vi là một thị trường." : "Budget approved, scope is one market.");
    }
    await expect(page.getByText("Proposal v1", { exact: true })).toBeVisible();
    const unapproved = (await (await page.request.get("/api/v1/projects?page_size=100")).json()).items;
    expect(unapproved.map((p: {id: string}) => p.id)).toEqual(before.map((p: {id: string}) => p.id));
    await page.getByRole("button", { name: l("assistant.proposal.approve"), exact: true }).click();
    await expect(page.getByRole("heading", { name: l("assistant.decision.title") })).toBeVisible();
    const conversation = new URL(page.url()).searchParams.get("conversation");
    const after = (await (await page.request.get("/api/v1/projects?page_size=100")).json()).items;
    const project = after.find((p: {id: string}) => !before.some((old: {id: string}) => old.id === p.id));
    expect(project).toBeTruthy();
    const name = `Core MVP ${locale} ${Date.now()}`;
    const renamed = await page.request.patch(`/api/v1/projects/${project.id}`, { data: { name }, headers: { ...headers(), "If-Match": `"${project.version}"` } });
    expect(renamed.status()).toBe(200);
    const tasks = (await (await page.request.get(`/api/v1/tasks?project_id=${project.id}`)).json()).items;
    expect(tasks).toHaveLength(2);
    expect(tasks.every((t: {assignee: unknown}) => t.assignee === null)).toBe(true);
    const taskRename = await page.request.patch(`/api/v1/tasks/${tasks[0].id}`, {data: {title: `Survey ${name}`}, headers: {...headers(), "If-Match": `"${tasks[0].version}"`}});
    expect(taskRename.status()).toBe(200);
    tasks[0] = await taskRename.json();
    await send(page, locale, locale === "vi" ? `Trạng thái dự án "${name}"?` : `Project status for "${name}"?`);
    await expect(page.getByRole("region", { name: l("assistant.report.statusTitle"), exact: true })).toBeVisible();

    await page.getByRole("button", { name: "Projects", exact: true }).click();
    await page.getByRole("button", { name: new RegExp(name) }).click();
    await page.getByRole("tab", { name: locale === "vi" ? "Đội ngũ" : "Team", exact: true }).click();
    await page.getByRole("button", { name: l("projectTeam.actions.derive"), exact: true }).click();
    await page.getByRole("button", { name: l("projectTeam.actions.confirm"), exact: true }).click();
    await page.getByRole("button", { name: l("projectTeam.recommendation.create"), exact: true }).click();
    await page.getByRole("button", { name: l("projectTeam.recommendation.actions.approve"), exact: true }).click();
    await page.getByRole("button", { name: l("projectTeam.teamDecisionConfirmApprove"), exact: true }).click();
    await expect(page.getByRole("heading", { name: l("projectTeam.currentTeamTitle") })).toBeVisible();
    const still = (await (await page.request.get(`/api/v1/tasks/${tasks[0].id}`)).json());
    expect(still.assignee).toBeNull();
    await page.getByRole("tab", { name: "Tasks", exact: true }).click();
    await page.getByRole("button", { name: tasks[0].title, exact: false }).click();
    await page.getByLabel(l("projectTeam.assignment.title")).getByRole("button", { name: l("projectTeam.assignment.action.open"), exact: true }).click();
    const members = page.getByLabel(l("projectTeam.assignment.memberLabel"));
    await members.selectOption({ index: 1 });
    const memberName = (await members.locator("option:checked").textContent())!.trim();
    await page.getByRole("button", { name: l("projectTeam.assignment.action.confirm").replace("{name}", memberName), exact: true }).click();
    await expect(page.getByText(l("projectTeam.assignment.success").replace("{name}", memberName), { exact: true })).toBeVisible();
    const assigned = await (await page.request.get(`/api/v1/tasks/${tasks[0].id}`)).json();
    expect(assigned.assignee).not.toBeNull();
    const backend = resolve(process.cwd(), "../backend");
    const employeeEmail = execFileSync("uv", ["run", "--directory", backend, "python", resolve(process.cwd(), "e2e/fixtures/reporting-seed.py"), "--employee", assigned.assignee.membership_id], {env: {...process.env, PYTHONPATH: backend, APP_DATABASE_URL: "postgresql+psycopg://work_management:work_management@localhost:5432/work_management_e2e"}, timeout: 60_000}).toString().trim();
    expect(employeeEmail).toMatch(/@example\.test$/);
    await logout(page);
    await login(page, employeeEmail, locale);
    await page.getByRole("button", { name: tasks[0].title, exact: false }).click();
    await page.getByRole("button", { name: l("work.task.start"), exact: true }).click();
    const form = page.locator("#daily-update-form");
    await form.getByLabel(l("dailyUpdate.percent"), { exact: true }).fill("50");
    await form.getByLabel(l("dailyUpdate.done"), { exact: true }).fill(locale === "vi" ? "Đã khảo sát" : "Completed survey");
    await form.getByLabel(locale === "vi" ? "Mô tả vướng mắc" : "Blocker description", { exact: true }).fill("Awaiting materials");
    await form.getByLabel(locale === "vi" ? "Mức độ" : "Severity", { exact: true }).selectOption("HIGH");
    await form.getByRole("button", { name: locale === "vi" ? "Thêm vào báo cáo" : "Add to report", exact: true }).click();
    const proof = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4//8/AAX+Av4N70a4AAAAAElFTkSuQmCC", "base64");
    await form.getByLabel(locale === "vi" ? "Chọn bằng chứng" : "Choose evidence", { exact: true }).setInputFiles({name: "proof.png", mimeType: "image/png", buffer: proof});
    await expect(form.getByRole("link", { name: locale === "vi" ? "Tải xuống" : "Download", exact: true })).toBeVisible();
    await form.getByRole("group", { name: l("dailyUpdate.selectedEvidence") }).getByRole("checkbox").check();
    await form.getByRole("button", { name: l("dailyUpdate.review"), exact: true }).click();
    expect(await (await page.request.get(`/api/v1/daily-updates?task_id=${assigned.id}`)).json()).toHaveLength(0);
    await form.getByRole("button", { name: l("dailyUpdate.assessment.request"), exact: true }).click();
    await form.getByRole("checkbox", { name: l("dailyUpdate.assessment.acknowledge"), exact: true }).check();
    await form.getByRole("button", { name: l("dailyUpdate.assessment.confirmDespiteWarning"), exact: true }).click();
    await expect(form.getByText(l("dailyUpdate.saved"), { exact: true })).toBeVisible();
    expect(await (await page.request.get(`/api/v1/blockers?task_id=${assigned.id}`)).json()).toHaveLength(1);
    const taskAfter = await (await page.request.get(`/api/v1/tasks/${assigned.id}`)).json();
    expect(taskAfter.assignee).toEqual(assigned.assignee);
    expect(taskAfter.status).toBe("IN_PROGRESS");
    await page.getByRole("navigation").getByRole("button", { name: l("assistant.conversations.new"), exact: true }).click();
    await send(page, locale, locale === "vi" ? `Tạo báo cáo cho dự án "${name}"` : `Generate a report for project "${name}"`);
    await expect(page.getByRole("heading", { name: l("assistant.unavailable.title") })).toBeVisible();
    expect((await page.request.get(`/api/v1/reports/${crypto.randomUUID()}`)).status()).toBe(403);
    await logout(page);
    await login(page, "manager@example.test", locale);
    // Let the native reconciliation finish before explicitly seeding a stored synthetic AI judgment.
    await expect.poll(async () => (await (await page.request.get(`/api/v1/risks?task_id=${assigned.id}`)).json()).state, {timeout: 60_000}).toBe("UNAVAILABLE");
    execFileSync("uv", ["run", "--directory", backend, "python", resolve(process.cwd(), "e2e/fixtures/risk-seed.py"), assigned.id], {env: {...process.env, PYTHONPATH: backend, APP_DATABASE_URL: "postgresql+psycopg://work_management:work_management@localhost:5432/work_management_e2e"}, timeout: 60_000});
    await expect.poll(async () => (await (await page.request.get(`/api/v1/risks?task_id=${assigned.id}`)).json()).state, {timeout: 60_000}).toBe("READY");
    const risk = await (await page.request.get(`/api/v1/risks?task_id=${assigned.id}`)).json();
    expect(Number(risk.judgment.score)).toBeGreaterThanOrEqual(0);
    expect(Number(risk.judgment.score)).toBeLessThanOrEqual(100);
    expect((await (await page.request.get(`/api/v1/risks?task_id=${assigned.id}`)).json()).id).toBe(risk.id);
    // A bounded fixture gives the Manager a real owned task; never reports as the Employee.
    const owned = JSON.parse(execFileSync("uv", ["run", "--directory", backend, "python", resolve(process.cwd(), "e2e/fixtures/reporting-seed.py"), project.id], { env: {...process.env, PYTHONPATH: backend, APP_DATABASE_URL: "postgresql+psycopg://work_management:work_management@localhost:5432/work_management_e2e"}, timeout: 60_000 }).toString());
    await page.goto(`/?conversation=${conversation}`);
    await send(page, locale, locale === "vi" ? `Cập nhật hôm nay cho ${owned.title}: Đã khảo sát, tiến độ 50%.` : `Daily update for ${owned.title}: Completed survey, progress 50%.`);
    await expect(page.getByText(l("dailyUpdate.chat.review"), { exact: true })).toBeVisible();
    await page.getByRole("button", { name: l("dailyUpdate.confirm"), exact: true }).click();
    await expect(page.getByText(l("dailyUpdate.saved"), { exact: true })).toBeVisible();
    await send(page, locale, locale === "vi" ? `Tạo báo cáo cho dự án "${name}"` : `Generate a report for project "${name}"`);
    await expect(page.getByRole("region", { name: l("assistant.report.title"), exact: true })).toBeVisible();
    expect(new URL(page.url()).searchParams.get("conversation")).toBe(conversation);
    const handoffs = JSON.parse(execFileSync("uv", ["run", "--directory", backend, "python", resolve(process.cwd(), "e2e/fixtures/reporting-seed.py"), "--conversation", conversation!], {env: {...process.env, PYTHONPATH: backend, APP_DATABASE_URL: "postgresql+psycopg://work_management:work_management@localhost:5432/work_management_e2e"}, timeout: 60_000}).toString());
    expect(handoffs.peer_handoff_count).toBe(0);
    expect(handoffs.cross_tenant_leakage_count).toBe(0);
    await page.getByRole("button", { name: "Projects", exact: true }).click();
    await page.getByRole("button", { name: new RegExp(name) }).click();
    await page.getByRole("tab", { name: l("work.project.planTab"), exact: true }).click();
    await expect(page.getByRole("heading", { name: l("weeklyProgress.title") })).toBeVisible();
    await page.getByRole("tab", { name: locale === "vi" ? "Báo cáo" : "Reports", exact: true }).click();
    let feedbackId: string | undefined;
    for (const kind of ["DAILY", "WEEKLY"] as const) {
      await page.getByRole("button", { name: locale === "vi" ? "Tạo báo cáo" : "Create report", exact: true }).click();
      if (kind === "WEEKLY") await page.getByLabel(l("reports.kind")).selectOption(kind);
      const response = page.waitForResponse(r => r.url().endsWith("/api/v1/reports") && r.request().method() === "POST");
      await page.getByRole("button", { name: locale === "vi" ? "Tạo báo cáo số liệu" : "Generate report", exact: true }).click();
      const report = await (await response).json();
      expect(report.report.kind).toBe(kind);
      await expect(page.getByRole("button", { name: locale === "vi" ? "Duyệt và xuất bản" : "Review and publish", exact: true })).toBeEnabled();
      if (kind === "WEEKLY") {
        await page.getByLabel(locale === "vi" ? "Lý do từ chối" : "Reason for rejection").fill("Needs more context");
        const rejectedResponse = page.waitForResponse(r => r.url().endsWith(`/reports/${report.report.id}/review-decisions`) && r.request().method() === "POST");
        await page.getByRole("button", { name: locale === "vi" ? "Từ chối nhận xét" : "Reject narrative", exact: true }).click();
        expect((await rejectedResponse).status()).toBe(201);
        const rejected = await (await page.request.get(`/api/v1/reports/${report.report.id}`)).json();
        expect(rejected.publications).toHaveLength(0);
        expect(rejected.review_state).toBe("REJECTED");
      } else {
        await page.getByRole("button", { name: locale === "vi" ? "Sửa nhận xét" : "Edit narrative", exact: true }).click();
        await page.getByRole("button", { name: locale === "vi" ? "Thêm giới hạn" : "Add limitation", exact: true }).click();
        await page.getByRole("textbox", { name: new RegExp(locale === "vi" ? "Nhận xét · note_" : "Narrative · note_") }).fill("Only captured data is included.");
        await page.getByRole("button", { name: locale === "vi" ? "Lưu và kiểm chứng" : "Save and verify", exact: true }).click();
        await expect(page.getByRole("button", { name: locale === "vi" ? "Duyệt và xuất bản" : "Review and publish", exact: true })).toBeEnabled();
        await page.getByRole("button", { name: locale === "vi" ? "Duyệt và xuất bản" : "Review and publish", exact: true }).click();
        await expect(page.getByText(locale === "vi" ? "Bản xuất bản hiện tại" : "Current publication", { exact: true })).toBeVisible();
        const published = await (await page.request.get(`/api/v1/reports/${report.report.id}`)).json();
        expect(published.feedback[0].decision).toBe("EDIT");
        feedbackId = published.feedback[0].id;
        const outcomes = await page.request.post(`/api/v1/feedback/${published.feedback[0].id}/outcomes`, {data: {source_type: "TASK_ACTUALS", source_id: assigned.id, source_version: 1}, headers: headers()});
        expect(outcomes.status(), await outcomes.text()).toBe(201);
        expect((await outcomes.json()).state).toBe("AVAILABLE");
      }
    }
    await logout(page);
    await login(page, "admin@example.test", locale);
    expect(feedbackId).toBeTruthy();
    const dataset = execFileSync("uv", ["run", "--directory", backend, "python", resolve(process.cwd(), "e2e/fixtures/evaluation-seed.py"), feedbackId!], {env: {...process.env, PYTHONPATH: backend, APP_DATABASE_URL: "postgresql+psycopg://work_management:work_management@localhost:5432/work_management_e2e", APP_AI_PROVIDER: "mock"}, timeout: 60_000}).toString().trim();
    await page.getByRole("button", {name: locale === "vi" ? "Đánh giá AI" : "AI evaluation", exact: true}).click();
    await page.getByLabel(locale === "vi" ? "ID phiên bản dataset" : "Dataset version ID").fill(dataset);
    await page.getByRole("button", {name: locale === "vi" ? "Chạy đánh giá" : "Run evaluation", exact: true}).click();
    await expect(page.getByText(locale === "vi" ? "Gate chưa đạt" : "Gate failed", {exact: true})).toBeVisible({timeout: 60_000});
    await expect(page.getByText("2 / 2", {exact: true})).toBeVisible();
  });
}
