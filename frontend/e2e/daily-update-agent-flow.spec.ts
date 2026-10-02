import { execFileSync } from "node:child_process";
import { resolve } from "node:path";
import { expect,test } from "@playwright/test";

test("Employee reviews an AI daily draft, edits evidence, acknowledges assessment and confirms",async({page})=>{
 const backend=resolve(process.cwd(),"../backend");
 const fixtureId=execFileSync("uv",["run","--directory",backend,"python",resolve(process.cwd(),"e2e/fixtures/daily-update-seed.py")],{env:{...process.env,PYTHONPATH:backend,APP_DATABASE_URL:"postgresql+psycopg://work_management:work_management@localhost:5432/work_management_e2e"}}).toString().trim();
 await page.goto("/login");await page.getByLabel("Email").fill("employee@example.test");await page.getByLabel("Mật khẩu").fill("WorkDemo123!");await page.getByRole("button",{name:"Đăng nhập"}).click();
 await expect(page.getByRole("button",{name:"Đăng xuất"})).toBeVisible();
 const task=await(await page.request.get(`/api/v1/tasks/${fixtureId}`)).json();
 await page.getByRole("navigation",{name:"Điều hướng chính"}).getByRole("button",{name:"Cuộc trò chuyện mới"}).click();
 await page.getByLabel("Nhắn cho Trợ lý AI").fill(`Cập nhật hôm nay cho ${task.title}: Đã khảo sát, tiến độ 50%.`);
 await page.getByRole("button",{name:"Gửi",exact:true}).click();
 await expect(page.getByText("Kiểm tra bản nháp do AI đề xuất trước khi xác nhận.")).toBeVisible({timeout:20000});
 const before=await(await page.request.get(`/api/v1/daily-updates?task_id=${task.id}`)).json();expect(before).toHaveLength(0);
 await expect(page.getByText("50%",{exact:true})).toBeVisible();
 await page.getByRole("button",{name:"Sửa báo cáo",exact:true}).click();
 const image=execFileSync("uv",["run","--directory",backend,"python","-c","from PIL import Image; import sys; Image.new('RGB',(2,2),'white').save(sys.stdout.buffer,format='PNG')"]);
 const upload=page.waitForResponse(r=>r.request().method()==="POST"&&r.url().endsWith("/api/v1/evidence"));
 await page.getByLabel("Chọn bằng chứng",{exact:true}).setInputFiles({name:"original.png",mimeType:"image/png",buffer:image});
 const proof=await(await upload).json();await page.getByRole("checkbox",{name:`${proof.evidence_id} · v1`,exact:true}).check();
 const revised=page.waitForResponse(r=>r.request().method()==="PATCH"&&r.url().endsWith("/draft"));
 await page.getByRole("button",{name:"Xem lại báo cáo",exact:true}).click();const revision=await revised;expect(revision.status()).toBe(200);
 const durable=await revision.json();
 await page.getByLabel("Nhắn cho Trợ lý AI").fill(`Cập nhật bản nháp ${durable.id}: Đã khảo sát, tiến độ 50%.`);
 await page.getByRole("button",{name:"Gửi",exact:true}).click();
 await expect(page.getByText("Kiểm tra bản nháp do AI đề xuất trước khi xác nhận.")).toHaveCount(2,{timeout:20000});
 const old=page.getByRole("button",{name:"Bỏ bản nháp này",exact:true});
 if(await old.count())await old.first().click();


 await expect(page.getByText("Một số nội dung hoặc nguồn chưa thể đánh giá.")).toBeVisible();
 await expect(page.getByRole("button",{name:"Vẫn gửi dù có cảnh báo",exact:true})).toBeDisabled();
 await page.getByRole("checkbox",{name:"Tôi đã đọc các cảnh báo này và vẫn muốn gửi báo cáo.",exact:true}).check();
 await page.getByRole("button",{name:"Vẫn gửi dù có cảnh báo",exact:true}).click();
 await expect(page.getByText("Đã xác nhận báo cáo",{exact:true})).toBeVisible();
 const after=await(await page.request.get(`/api/v1/daily-updates?task_id=${task.id}`)).json();expect(after).toHaveLength(1);expect(Number(after[0].item.reported_percent)).toBe(50);
 const unchanged=await(await page.request.get(`/api/v1/tasks/${task.id}`)).json();expect(unchanged.status).toBe(task.status);expect(unchanged.version).toBe(task.version);
});
