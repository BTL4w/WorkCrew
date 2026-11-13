import { expect, test } from "@playwright/test";

for (const [index, locale] of (["vi", "en"] as const).entries()) {
  test(`${locale}: committed SSE replay preserves cursor and rendered messages`, async ({ page }) => {
    await page.addInitScript(language => localStorage.setItem("work-management-locale", language), locale);
    await page.goto("/login");
    await page.getByLabel("Email").fill(`bench-0-${index + 4}@example.test`);
    await page.getByLabel(locale === "vi" ? "Mật khẩu" : "Password").fill("RedisBenchmarkOnly-2026!");
    const loggedIn = page.waitForResponse(r => r.url().endsWith("/api/v1/auth/login"));
    await page.getByRole("button", { name: locale === "vi" ? "Đăng nhập" : "Sign in", exact: true }).click();
    expect((await loggedIn).status()).toBe(200);
    await page.goto("/?view=assistant");
    await page.getByRole("button", { name: locale === "vi" ? "Cuộc trò chuyện mới" : "New conversation", exact: true }).first().click();
    const composer = page.getByRole("textbox", { name: locale === "vi" ? "Nhắn cho Trợ lý AI" : "Message the AI Assistant" });
    const text = `SSE replay fixture ${locale} ${crypto.randomUUID()}`;
    await composer.fill(text);
    const accepted = page.waitForResponse(r => r.request().method() === "POST" && /\/conversations\/[^/]+\/messages$/.test(new URL(r.url()).pathname));
    await page.getByRole("button", { name: locale === "vi" ? "Gửi" : "Send", exact: true }).click();
    const response = await accepted;
    expect(response.status()).toBe(202);
    const path = new URL(response.url()).pathname.replace(/\/messages$/, "");
    const before = await (await page.request.get(path)).json();
    // Use the browser's authenticated streaming fetch, including Last-Event-ID.
    // Abort after one frame; reopening performs a read, with no new POST/job.
    const frames = await page.evaluate(async ({ path }) => {
      async function readAfter(cursor: number) {
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 20_000);
        try {
          const response = await fetch(`${path}/events`, { headers: { "Last-Event-ID": String(cursor) }, signal: controller.signal });
          if (!response.ok || !response.body) throw new Error("SSE_READ_FAILED");
          const reader = response.body.getReader();
          let data = "";
          while (true) {
            const next = await reader.read().catch(error => { throw new Error(`SSE read diagnostic: ${response.headers.get("content-type")} bytes=${data.length} prefix=${JSON.stringify(data.slice(0, 160))}`, { cause: error }); });
            if (next.done) throw new Error("EXPECTED_PERSISTED_FRAME");
            data += new TextDecoder().decode(next.value);
            for (const frame of data.split("\n\n").slice(0, -1)) {
              if (frame.startsWith("id:")) return frame;
            }
          }
        } finally {
          clearTimeout(timer);
          controller.abort();
        }
      }
      return [await readAfter(0), await readAfter(0)];
    }, { path });
    expect(frames[0]).toBe(frames[1]);
    expect(frames[0]).toContain("event: assistant.turn.queued.v1");
    const after = await (await page.request.get(path)).json();
    expect(after).toEqual(before);
    expect(after.messages.map((message: { id: string }) => message.id)).toEqual(before.messages.map((message: { id: string }) => message.id));
    await expect(page.getByText(text, { exact: true })).toHaveCount(1);
    await page.reload();
    await expect(page.getByText(text, { exact: true })).toHaveCount(1);
  });
}
