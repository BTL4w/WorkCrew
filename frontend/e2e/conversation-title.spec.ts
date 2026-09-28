import { expect, test } from "@playwright/test";

test("first message names the conversation and later messages preserve the title", async ({ page }) => {
  await page.goto("/login");
  await page.getByLabel("Email").fill("employee@example.test");
  await page.getByLabel("Mật khẩu").fill("WorkDemo123!");
  await page.getByRole("button", { name: "Đăng nhập" }).click();
  await expect(page.getByRole("button", { name: "Đăng xuất" })).toBeVisible();
  await page.getByRole("navigation", { name: "Điều hướng chính" })
    .getByRole("button", { name: "Cuộc trò chuyện mới" }).click();

  const firstMessage = "Công việc hiện tại của tôi";
  await page.getByLabel("Nhắn cho Trợ lý AI").fill(firstMessage);
  await page.getByRole("button", { name: "Gửi", exact: true }).click();
  await expect(page.locator(".assistant-history").getByRole("button", { name: firstMessage })).toBeVisible();
  const conversationId = new URL(page.url()).searchParams.get("conversation");
  expect(conversationId).toBeTruthy();

  await page.getByLabel("Nhắn cho Trợ lý AI").fill("Một chủ đề khác về báo cáo");
  await page.getByRole("button", { name: "Gửi", exact: true }).click();
  await expect.poll(async () => {
    const response = await page.request.get(`/api/v1/ai/conversations/${conversationId}`);
    const snapshot = await response.json();
    return snapshot.messages.filter((message: { role: string }) => message.role === "USER").length;
  }).toBe(2);
  await page.reload();
  await expect(page.locator(".assistant-history").getByRole("button", { name: firstMessage })).toBeVisible();
  const response = await page.request.get(`/api/v1/ai/conversations/${conversationId}`);
  expect((await response.json()).conversation.title).toBe(firstMessage);
});
