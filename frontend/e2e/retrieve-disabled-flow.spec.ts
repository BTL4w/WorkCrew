import { expect, test } from "@playwright/test";

for (const locale of ["vi","en"] as const) {
  test(`disabled providers keep manual project/task and chat fallback usable in ${locale}`, async ({page}) => {
    await page.goto("/login");
    await page.getByLabel("Email").fill("manager@example.test");
    await page.getByLabel("Mật khẩu").fill("WorkDemo123!");
    await page.getByRole("button",{name:"Đăng nhập",exact:true}).click();
    await expect(page.getByRole("button",{name:"Đăng xuất"})).toBeVisible();
    if(locale==="en") await page.getByRole("button",{name:"en",exact:true}).click();
    const composer=page.getByLabel(locale==="vi"?"Nhắn cho Trợ lý AI":"Message the AI Assistant");
    await composer.fill(locale==="vi"?"Task của tôi hôm nay?":"What are my tasks today?");
    await page.getByRole("button",{name:locale==="vi"?"Gửi":"Send",exact:true}).click();
    await page.getByRole("button", {name:locale === "vi" ? "Tiếp tục thủ công" : "Continue manually",exact:true}).click();
    await expect(page.getByRole("region",{name:locale==="vi"?"Nghiên cứu web":"Web research"})).toHaveCount(0);
    const label = (vi: string, en: string) => locale === "vi" ? vi : en;
    await page.getByRole("navigation").getByRole("button",{name:"Projects",exact:true}).click();
    await page.getByRole("button",{name:label("Tạo project", "Create project"),exact:true}).click();
    const name=`Offline retrieval ${locale} ${Date.now()}`;
    await page.getByLabel(label("Tên project", "Project name")).fill(name);
    await page.getByLabel(label("Mô tả project", "Project description")).fill("Synthetic offline browser acceptance");
    await page.getByRole("button",{name:label("Lưu project", "Save project"),exact:true}).click();
    await expect(page.getByRole("heading",{name,exact:true})).toBeVisible();
    await page.getByRole("tab", {name:label("Kế hoạch", "Plan"),exact:true}).click();
    await page.getByRole("button", {name:label("Thêm tuần", "Add week"),exact:true}).click();
    await page.getByLabel(label("Ngày bắt đầu", "Start date")).fill("2026-10-26");
    await page.getByLabel(label("Ngày kết thúc", "End date")).fill("2026-11-01");
    await page.getByLabel(label("Mục tiêu tuần", "Week objective")).fill("Synthetic offline acceptance");
    await page.getByRole("button", {name:label("Lưu tuần", "Save week"),exact:true}).click();
    await expect(page.getByText("Synthetic offline acceptance",{exact:true})).toBeVisible();
    await page.getByRole("tab",{name:"Tasks",exact:true}).click();
    await page.getByRole("button",{name:label("Tạo task", "Create task"),exact:true}).click();
    await page.getByLabel(label("Tiêu đề task", "Task title")).fill(name+" task");
    await page.getByLabel(label("Tuần dự án", "Project week")).selectOption({label:label("Tuần 1", "Week 1")});
    await page.getByLabel(label("Kỹ năng cần thiết, mỗi dòng một mục", "Required skills, one per line")).fill("Manual Testing");
    await page.getByLabel(label("Số giờ công ước tính", "Estimated effort hours")).fill("1");
    await page.getByRole("button",{name:label("Lưu task", "Save task"),exact:true}).click();
    await expect(page.getByRole("heading",{name:name+" task",exact:true})).toBeVisible();
  });
}
