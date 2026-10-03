import { execFileSync } from "node:child_process";
import { resolve } from "node:path";
import { expect, test } from "@playwright/test";

test("Manager reviews immutable AI risk and reads a deduplicated notification", async ({ page }) => {
 await page.goto("/login");
 await page.getByLabel("Email").fill("manager@example.test");
 await page.getByLabel("Mật khẩu").fill("WorkDemo123!");
 await page.getByRole("button", { name: "Đăng nhập" }).click();
 await expect(page.getByRole("button", { name: "Đăng xuất" })).toBeVisible();
 const key = () => crypto.randomUUID();
 const projectResponse = await page.request.post("/api/v1/projects", { headers: { "Idempotency-Key": key() }, data: { name: `Risk review ${Date.now()}`, description: "Risk browser flow" } });
 expect(projectResponse.status()).toBe(201); const project = await projectResponse.json();
 const weekResponse = await page.request.post(`/api/v1/projects/${project.id}/weeks`, { headers: { "Idempotency-Key": key() }, data: { week_number: 1, start_date: "2026-09-28", end_date: "2026-10-04", objective: "Delivery review" } });
 expect(weekResponse.status()).toBe(201); const week = await weekResponse.json();
 const taskResponse = await page.request.post("/api/v1/tasks", { headers: { "Idempotency-Key": key() }, data: { project_id: project.id, project_week_id: week.id, title: `Prepare shipment ${Date.now()}`, description: "Delivery review", estimated_effort_hours: 8 } });
 expect(taskResponse.status()).toBe(201); const task = await taskResponse.json();
 execFileSync("uv", ["run", "--directory", resolve(process.cwd(), "../backend"), "python", resolve(process.cwd(), "e2e/fixtures/risk-seed.py"), task.id], { env: { ...process.env, PYTHONPATH: resolve(process.cwd(), "../backend"), APP_DATABASE_URL: "postgresql+psycopg://work_management:work_management@localhost:5432/work_management_e2e" } });
 const assessment = await (await page.request.get(`/api/v1/risks?task_id=${task.id}`)).json();
 expect(assessment.judgment.score).toBe("83");
 await page.getByRole("button", { name: "Projects", exact: true }).click();
 await page.getByText(/Thông báo rủi ro/).click();
 const notice = await (await page.request.get("/api/v1/notifications")).json();
 const found = notice.find((n: { task_id: string }) => n.task_id === task.id); expect(found).toBeTruthy();
 await page.request.post(`/api/v1/notifications/${found.id}/read`, { headers: { "Idempotency-Key": key() } });
 // Notification navigation works across project pages and uses current API permissions.
 await page.getByRole("button", { name: "Mở công việc", exact: true }).first().click();
 await expect(page.getByRole("heading", { name: "Đánh giá rủi ro bằng AI" })).toBeVisible();
 await expect(page.getByText("83 / 100", { exact: true })).toBeVisible();
 await expect(page.getByText("Cần kiểm tra hạn hoàn thành và blocker.", { exact: true })).toBeVisible();
 await page.getByLabel("Nhận định của Manager", { exact: true }).selectOption("ACCEPTED_EXPLANATION");
 await page.getByLabel("Lý do nhận định", { exact: true }).fill("Đã xác nhận với người phụ trách.");
 await page.getByRole("button", { name: "Ghi nhận phản hồi", exact: true }).click();
 await expect(page.getByText(/Chấp nhận giải thích · Đã xác nhận với người phụ trách/)).toBeVisible();
 const after = await (await page.request.get(`/api/v1/risks?task_id=${task.id}`)).json();
 expect(after.id).toBe(assessment.id); expect(after.judgment.score).toBe("83");
 expect((await (await page.request.get(`/api/v1/tasks/${task.id}`)).json()).version).toBe(task.version);
});
