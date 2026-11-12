import { execFileSync } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { expect, test } from "@playwright/test";

const outage = process.env.REDIS_E2E_OUTAGE === "1";
const receiptsPath = process.env.REDIS_E2E_RECEIPTS;
if (!receiptsPath) throw new Error("SYNTHETIC_RECEIPTS_PATH_REQUIRED");
type Receipt = { path: string; body: { message: string; locale: string }; key: string; turn: string };

for (const [index, locale] of (["vi", "en"] as const).entries()) {
  test(`${locale}: admission retains draft, exact retry and accepted receipts`, async ({ page }) => {
    await page.addInitScript(language => localStorage.setItem("work-management-locale", language), locale);
    await page.goto("/login");
    await page.getByLabel("Email").fill(`bench-0-${index + 2}@example.test`);
    await page.getByLabel(locale === "vi" ? "Mật khẩu" : "Password").fill("RedisBenchmarkOnly-2026!");
    const login = page.waitForResponse(r => r.url().endsWith("/api/v1/auth/login"));
    await page.getByRole("button", { name: locale === "vi" ? "Đăng nhập" : "Sign in", exact: true }).click();
    expect((await login).status()).toBe(200);
    await page.goto("/?view=assistant");
    await page.getByRole("button", { name: locale === "vi" ? "Cuộc trò chuyện mới" : "New conversation", exact: true }).first().click();
    const composer = page.getByRole("textbox", { name: locale === "vi" ? "Nhắn cho Trợ lý AI" : "Message the AI Assistant" });
    const send = page.getByRole("button", { name: locale === "vi" ? "Gửi" : "Send", exact: true });
    const messageResponse = () => page.waitForResponse(r => r.request().method() === "POST" && /\/ai\/conversations\/[^/]+\/messages$/.test(new URL(r.url()).pathname));
    let receipt: Receipt;
    if (!outage) {
      await composer.fill("Summarize my tasks");
      const response = messageResponse();
      await send.click();
      const accepted = await response;
      expect(accepted.status()).toBe(202);
      receipt = { path: new URL(accepted.url()).pathname, body: accepted.request().postDataJSON(), key: accepted.request().headers()["idempotency-key"], turn: (await accepted.json()).turn_id };
      await expect(composer).toHaveValue("");
      let saved: Record<string, Receipt> = {};
      try { saved = JSON.parse(readFileSync(receiptsPath!, "utf8")); } catch { /* First synthetic receipt. */ }
      writeFileSync(receiptsPath!, JSON.stringify({ ...saved, [locale]: receipt }));
    } else {
      receipt = JSON.parse(readFileSync(receiptsPath!, "utf8"))[locale];
      expect(receipt).toBeTruthy();
    }
    await composer.fill("Plan an event next week");
    const response = messageResponse();
    await send.click();
    const rejected = await response;
    expect(rejected.status()).toBe(outage ? 503 : 429);
    const retryKey = rejected.request().headers()["idempotency-key"];
    await expect(page.getByRole("region", { name: locale === "vi" ? "Trợ lý AI" : "AI Assistant", exact: true }).getByRole("alert")).toContainText(locale === "vi"
      ? outage ? "Tạm thời chưa thể nhận yêu cầu AI mới" : "Đã đạt hạn mức yêu cầu AI"
      : outage ? "New AI requests are temporarily unavailable" : "AI request limit reached");
    await expect(composer).toHaveValue("Plan an event next week");
    const replay = await page.request.post(receipt.path, { data: receipt.body, headers: { "Idempotency-Key": receipt.key } });
    expect(replay.status()).toBe(202);
    expect((await replay.json()).turn_id).toBe(receipt.turn);
    expect(replay.headers()["idempotency-replayed"]).toBe("true");
    if (!outage) {
      const me = await (await page.request.get("/api/v1/me")).json();
      execFileSync("uv", ["run", "--locked", "python", "-m", "app.scripts.redis_e2e_server", "reset-test-limits", me.membership.organization_id, me.membership.id], { cwd: resolve(process.cwd(), "../backend"), env: process.env });
      const retry = messageResponse();
      await send.click();
      const recovered = await retry;
      expect(recovered.status()).toBe(202);
      expect(recovered.request().headers()["idempotency-key"]).toBe(retryKey);
      await expect(composer).toHaveValue("");
    } else {
      // The authenticated browser can still confirm an owner-written update.
      const tasks = await (await page.request.get("/api/v1/my-tasks")).json();
      const task = tasks.items[0];
      const context = await (await page.request.get(`/api/v1/tasks/${task.id}/reporting-context`)).json();
      const draft = await page.request.post("/api/v1/daily-updates/drafts", { data: { items: [{ task_id: task.id, expected_task_version: context.task_version, expected_progress_version: context.progress_version, reported_percent: "10", spent_hours: "1", reporting_date: context.reporting_date, done_text: "Manual update during admission outage" }] }, headers: { "Idempotency-Key": crypto.randomUUID() } });
      expect(draft.status()).toBe(201);
      const data = await draft.json();
      const confirmed = await page.request.post(`/api/v1/daily-updates/${data.id}/confirm`, { data: { draft_id: data.id, expected_draft_version: data.version }, headers: { "Idempotency-Key": crypto.randomUUID() } });
      expect(confirmed.status()).toBe(201);
    }
  });
}
