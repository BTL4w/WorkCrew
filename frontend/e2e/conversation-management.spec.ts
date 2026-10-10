import { expect, test } from "@playwright/test";

test("owner manages sidebar titles, pins and deletion on desktop and mobile", async ({ page }) => {
  await page.goto("/login");
  await page.getByLabel("Email").fill("employee@example.test");
  await page.getByLabel("Mật khẩu").fill("WorkDemo123!");
  await page.getByRole("button", { name: "Đăng nhập", exact: true }).click();
  await expect(page.getByRole("button", { name: "Đăng xuất", exact: true })).toBeVisible();
  const title = `Kiểm tra sidebar ${Date.now()} với tiêu đề dài có thể xem đầy đủ khi di chuột`;
  const created = await page.request.post("/api/v1/ai/conversations", {
    headers: { "Idempotency-Key": crypto.randomUUID() }, data: { locale: "vi", title },
  });
  expect(created.status()).toBe(201);
  const chat = await created.json() as { id: string };
  await page.reload();
  const sidebar = page.locator(".assistant-history");
  const row = sidebar.locator(".assistant-conversation-row").filter({ has: page.getByRole("button", { name: title, exact: true }) });
  await row.getByRole("button", { name: title, exact: true }).hover();
  const text = row.locator(".assistant-conversation-title-text");
  await expect(row.locator(".assistant-conversation-title-window")).toHaveClass(/is-scrolling/);
  await expect.poll(() => text.evaluate((element) => new DOMMatrixReadOnly(getComputedStyle(element).transform).m41)).toBeLessThan(-5);
  await expect(page.getByRole("tooltip")).toHaveCount(0);
  await row.getByRole("button", { name: "Ghim chat", exact: true }).click();
  await expect(sidebar.getByRole("heading", { name: "Đã ghim" })).toBeVisible();
  expect(await sidebar.evaluate((element) => {
    const pin = element.querySelector("#assistant-pinned-title")!;
    const recent = element.querySelector("#assistant-history-title")!;
    return Boolean(pin.compareDocumentPosition(recent) & Node.DOCUMENT_POSITION_FOLLOWING);
  })).toBe(true);
  await expect(row.getByRole("button", { name: "Bỏ ghim chat", exact: true })).toBeEnabled();
  await page.reload();
  await expect(row.getByRole("button", { name: "Bỏ ghim chat", exact: true })).toBeVisible();
  await row.getByRole("button", { name: "Tùy chọn chat" }).click();
  await page.getByRole("menuitem", { name: "Đổi tên", exact: true }).click();
  const renamed = `Tên đã sửa ${Date.now()}`;
  await page.getByLabel("Tên cuộc trò chuyện").fill(renamed);
  await page.getByRole("button", { name: "Lưu", exact: true }).click();
  await expect(sidebar.getByRole("button", { name: renamed, exact: true })).toBeVisible();
  await page.reload();
  await expect(sidebar.getByRole("button", { name: renamed, exact: true })).toBeVisible();
  await sidebar.getByRole("button", { name: renamed, exact: true }).click();
  await expect(page.getByLabel("Nhắn cho Trợ lý AI")).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  const toggle = page.getByRole("button", { name: "Ẩn hoặc hiện danh sách cuộc trò chuyện" });
  if (await toggle.getAttribute("aria-expanded") === "false") await toggle.click();
  const mobileRow = sidebar.locator(".assistant-conversation-row").filter({ has: page.getByRole("button", { name: renamed, exact: true }) });
  await mobileRow.getByRole("button", { name: "Tùy chọn chat" }).click();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("menu")).toHaveCount(0);
  await expect(toggle).toHaveAttribute("aria-expanded", "true");
  await mobileRow.getByRole("button", { name: "Bỏ ghim chat", exact: true }).click();
  await expect(mobileRow.getByRole("button", { name: "Ghim chat", exact: true })).toBeEnabled();
  await mobileRow.getByRole("button", { name: "Tùy chọn chat" }).click();
  await page.getByRole("menuitem", { name: "Xóa chat", exact: true }).click();
  await expect(page.getByRole("button", { name: "Hủy", exact: true })).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(toggle).toHaveAttribute("aria-expanded", "true");
  await expect(sidebar.getByRole("button", { name: renamed, exact: true })).toBeVisible();
  await mobileRow.getByRole("button", { name: "Tùy chọn chat" }).click();
  await page.getByRole("menuitem", { name: "Xóa chat", exact: true }).click();
  await page.getByRole("button", { name: "Xác nhận xóa", exact: true }).click();
  await expect(sidebar.getByRole("button", { name: renamed, exact: true })).toHaveCount(0);
  expect((await page.request.get(`/api/v1/ai/conversations/${chat.id}`)).status()).toBe(404);
  await toggle.click();
  await expect(page.getByRole("heading", { name: "Tôi có thể giúp gì cho bạn?" })).toBeVisible();
  expect(new URL(page.url()).searchParams.get("conversation")).toBeNull();
  await page.reload();
  await expect(sidebar.getByRole("button", { name: renamed, exact: true })).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test("unpinning an old chat restores its position below more recent chats", async ({ page }) => {
  await page.goto("/login");
  await page.getByLabel("Email").fill("employee@example.test");
  await page.getByLabel("Mật khẩu").fill("WorkDemo123!");
  await page.getByRole("button", { name: "Đăng nhập", exact: true }).click();
  await expect(page.getByRole("button", { name: "Đăng xuất", exact: true })).toBeVisible();
  const titles = [`Chat cũ ${Date.now()}`, `Chat mới ${Date.now()}`];
  const ids: string[] = [];
  try {
    for (const title of titles) {
      const created = await page.request.post("/api/v1/ai/conversations", {
        headers: { "Idempotency-Key": crypto.randomUUID() }, data: { locale: "vi", title },
      });
      expect(created.status()).toBe(201);
      ids.push((await created.json() as { id: string }).id);
    }
    await page.reload();
    const history = page.locator(".assistant-history");
    const old = history.locator(".assistant-conversation-row").filter({ has: page.getByRole("button", { name: titles[0], exact: true }) });
    const recentTitles = () => history.locator(".assistant-conversation-select").allTextContents();
    await expect(history.getByRole("button", { name: titles[1], exact: true })).toBeVisible();
    expect((await recentTitles()).indexOf(titles[1])).toBeLessThan((await recentTitles()).indexOf(titles[0]));
    await old.getByRole("button", { name: "Ghim chat", exact: true }).click();
    await expect(old.getByRole("button", { name: "Bỏ ghim chat", exact: true })).toBeEnabled();
    expect((await recentTitles()).indexOf(titles[0])).toBeLessThan((await recentTitles()).indexOf(titles[1]));
    await old.getByRole("button", { name: "Bỏ ghim chat", exact: true }).click();
    await expect(old.getByRole("button", { name: "Ghim chat", exact: true })).toBeEnabled();
    expect((await recentTitles()).indexOf(titles[1])).toBeLessThan((await recentTitles()).indexOf(titles[0]));
    await page.reload();
    await expect(history.getByRole("button", { name: titles[1], exact: true })).toBeVisible();
    expect((await recentTitles()).indexOf(titles[1])).toBeLessThan((await recentTitles()).indexOf(titles[0]));
  } finally {
    for (const id of ids) {
      const snapshot = await page.request.get(`/api/v1/ai/conversations/${id}`);
      if (snapshot.ok()) {
        const body = await snapshot.json() as { conversation: { version: number } };
        const deleted = await page.request.delete(`/api/v1/ai/conversations/${id}`, {
          headers: { "Idempotency-Key": crypto.randomUUID(), "If-Match": `"${body.conversation.version}"` },
        });
        expect(deleted.status()).toBe(200);
      }
    }
  }
});
