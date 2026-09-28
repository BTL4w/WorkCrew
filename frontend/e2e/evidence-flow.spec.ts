import { expect, test } from "@playwright/test";

// Valid 1x1 PNG original generated independently of the application parser.
const png = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4//8/AAX+Av4N70a4AAAAAElFTkSuQmCC", "base64");

test("employee uploads and downloads a private original; spoofed bytes are rejected", async ({ page }) => {
  await page.goto("/login");
  await page.getByLabel("Email").fill("employee@example.test");
  await page.getByLabel("Mật khẩu").fill("WorkDemo123!");
  await page.getByRole("button", { name: "Đăng nhập" }).click();
  await expect(page.getByRole("button", { name: "Đăng xuất" })).toBeVisible();
  const input = page.getByLabel("Chọn bằng chứng");
  await expect(input).toBeVisible();
  await input.setInputFiles({ name: "proof.png", mimeType: "image/png", buffer: png });
  const link = page.getByRole("link", { name: "Tải xuống", exact: true });
  await expect(link).toBeVisible();
  const response = await page.request.get((await link.getAttribute("href"))!);
  expect(response.status()).toBe(200);
  expect(await response.body()).toEqual(png);
  expect(response.headers()["cache-control"]).toBe("private, no-store");
  await input.setInputFiles({ name: "spoof.png", mimeType: "image/png", buffer: Buffer.from("not a PNG") });
  await expect(page.getByRole("alert").filter({ hasText: "Chọn tệp" })).toContainText("Chọn tệp PDF, DOCX, JPG hoặc PNG hợp lệ.");
});
