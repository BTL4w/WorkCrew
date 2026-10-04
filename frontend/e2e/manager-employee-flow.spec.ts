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

test("invalid credentials can be corrected without losing the login flow", async ({ page }) => {
  await page.goto("/login");
  await page.getByLabel("Email").fill("manager@example.test");
  await page.getByLabel("Mật khẩu").fill("incorrect-password");
  await page.getByRole("button", { name: "Đăng nhập" }).click();

  await expect(
    page.getByRole("alert").filter({ hasText: "Email hoặc mật khẩu không đúng." }),
  ).toHaveText("Email hoặc mật khẩu không đúng.");
  await expect(page).toHaveURL(/\/login$/);

  await page.getByLabel("Mật khẩu").fill(password);
  await page.getByRole("button", { name: "Đăng nhập" }).click();

  await expect(page.getByRole("button", { name: "Đăng xuất" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Projects" })).toBeVisible();
});

test("Manager assigns a Task and Employee reports evidence before completion", async ({ page }) => {
  const suffix = `${Date.now()}`;
  const projectName = `E2E Project ${suffix}`;
  const taskTitle = `E2E Task ${suffix}`;
  const assistantMutations: string[] = [];
  page.on("request", (request) => {
    if (request.method() === "POST" && request.url().includes("/api/v1/ai/")) {
      assistantMutations.push(request.url());
    }
  });

  await signIn(page, "manager@example.test");
  await page.getByRole("button", { name: "Tạo project" }).click();
  await page.getByLabel("Tên project").fill(projectName);
  await page.getByLabel("Mô tả project").fill("Weekly planning browser acceptance flow");
  await page.getByRole("button", { name: "Lưu project" }).click();
  await expect(page.getByRole("heading", { name: projectName })).toBeVisible();

  await page.getByRole("tab", { name: "Kế hoạch" }).click();
  await page.getByRole("button", { name: "Thêm mục tiêu" }).click();
  await page.getByLabel("Tiêu đề mục tiêu").fill(`Goal ${suffix}`);
  await page.getByLabel("Kết quả mong đợi").fill("Manual planning remains available");
  await page.getByRole("button", { name: "Lưu mục tiêu" }).click();
  await expect(page.getByText(`Goal ${suffix}`)).toBeVisible();

  await page.getByRole("button", { name: "Thêm milestone" }).click();
  await page.getByLabel("Tên milestone").fill(`Milestone ${suffix}`);
  await page.getByRole("button", { name: "Lưu milestone" }).click();
  await expect(page.getByText(`Milestone ${suffix}`)).toBeVisible();

  await page.getByRole("button", { name: "Thêm tuần" }).click();
  await page.getByLabel("Ngày bắt đầu").fill("2026-08-10");
  await page.getByLabel("Ngày kết thúc").fill("2026-08-16");
  await page.getByLabel("Mục tiêu tuần").fill("Complete the E2E task");
  await page.getByRole("button", { name: "Lưu tuần" }).click();
  await expect(page.getByText("Complete the E2E task")).toBeVisible();
  await page.getByRole("tab", { name: "Tasks" }).click();

  await page.getByRole("button", { name: "Tạo task" }).click();
  await page.getByLabel("Tiêu đề task").fill(taskTitle);
  await page.getByLabel("Tuần dự án").selectOption({ label: "Tuần 1" });
  await page.getByLabel("Kỹ năng cần thiết, mỗi dòng một mục").fill("Manual Testing");
  await page.getByLabel("Số giờ công ước tính").fill("8");
  await page.getByRole("button", { name: "Lưu task" }).click();
  await expect(page.getByRole("heading", { name: taskTitle })).toBeVisible();
  await expect(page.getByText("Chưa giao")).toBeVisible();

  await page.getByRole("button", { name: "← Quay lại" }).click();
  await page.getByRole("tab", { name: "Đội ngũ" }).click();
  await page.getByRole("button", { name: "Tạo yêu cầu từ task" }).click();
  await page.getByRole("button", { name: "Xác nhận yêu cầu" }).click();
  await page.getByRole("button", { name: "Tạo đề xuất đội ngũ" }).click();
  await expect(page.getByText("Đỗ Ngọc Nam").first()).toBeVisible();
  await page.getByRole("button", { name: "Phê duyệt đội ngũ" }).click();
  await page.getByRole("button", { name: "Xác nhận phê duyệt" }).click();
  await expect(page.getByRole("heading", { name: "Đội ngũ hiện tại" })).toBeVisible();

  await page.getByRole("tab", { name: "Tasks" }).click();
  await page.getByRole("button", { name: taskTitle }).click();
  await page.getByLabel("Giao task từ Project Team").getByRole("button", { name: "Giao task" }).click();
  const approvedMember = (await page.getByLabel("Thành viên dự án").locator("option").nth(1).textContent())?.trim() ?? "";
  expect(["Đỗ Ngọc Nam", "Bùi Thảo Nguyên"]).toContain(approvedMember);
  await page.getByLabel("Thành viên dự án").selectOption({ label: approvedMember });
  await page.getByRole("button", { name: `Xác nhận giao cho ${approvedMember}` }).click();
  await expect(page.getByText(`Đã giao cho ${approvedMember}`)).toBeVisible();
  expect(assistantMutations).toEqual([]);
  await signOut(page);

  await signIn(page, approvedMember === "Đỗ Ngọc Nam" ? "nam.do@example.test" : "nguyen.bui@example.test");
  await page.getByRole("button", { name: taskTitle }).click();
  await expect(page.getByRole("button", { name: "Sửa task" })).toHaveCount(0);
  await page.getByRole("button", { name: "Bắt đầu task" }).click();
  await expect(page.getByText("Đang thực hiện")).toBeVisible();
  await expect(page.getByRole("button", { name: "Hoàn tất" })).toBeDisabled();
  const proof = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4//8/AAX+Av4N70a4AAAAAElFTkSuQmCC", "base64");
  const reporting = page.locator("#daily-update-form");
  await reporting.getByLabel("Chọn bằng chứng").setInputFiles({
    name: "completion.png", mimeType: "image/png", buffer: proof,
  });
  await expect(reporting.getByRole("link", { name: "Tải xuống" })).toBeVisible();
  await reporting.getByLabel("Tiến độ báo cáo (%)").fill("100");
  await reporting.getByLabel("Công việc đã làm").fill("Completed the assigned work");
  await reporting.getByRole("group", { name: "Chọn minh chứng cho task này" }).getByRole("checkbox").check();
  await reporting.getByRole("button", { name: "Xem lại báo cáo" }).click();
  await reporting.getByRole("button", { name: "Xác nhận báo cáo" }).click();
  await expect(reporting.getByRole("alert")).toHaveText("Hãy đánh giá bằng chứng đã chọn trước khi gửi.");
  await reporting.getByRole("button", { name: "Đánh giá bằng chứng", exact: true }).click();
  await reporting.getByRole("checkbox", { name: "Tôi đã đọc các cảnh báo này và vẫn muốn gửi báo cáo.", exact: true }).check();
  await reporting.getByRole("button", { name: "Vẫn gửi dù có cảnh báo", exact: true }).click();
  await expect(reporting.getByRole("status")).toHaveText("Đã xác nhận báo cáo");
  await expect(page.getByRole("button", { name: "Hoàn tất" })).toBeEnabled();
  await page.getByRole("button", { name: "Hoàn tất" }).click();
  await expect(page.getByText("Hoàn thành", { exact: true })).toBeVisible();
  await signOut(page);

  await signIn(page, "manager@example.test");
  await page.getByRole("button", { name: projectName }).click();
  await page.getByRole("button", { name: taskTitle }).click();
  await expect(page.getByText("Hoàn thành", { exact: true })).toBeVisible();
});
