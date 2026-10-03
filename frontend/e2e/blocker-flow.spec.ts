import { execFileSync } from "node:child_process";
import { resolve } from "node:path";
import { expect,test } from "@playwright/test";

test("Employee confirms a draft blocker then acknowledges, resolves and reopens",async({page})=>{
 const backend=resolve(process.cwd(),"../backend");
 const fixtureId=execFileSync("uv",["run","--directory",backend,"python",resolve(process.cwd(),"e2e/fixtures/daily-update-seed.py")],{env:{...process.env,PYTHONPATH:backend,APP_DATABASE_URL:"postgresql+psycopg://work_management:work_management@localhost:5432/work_management_e2e"}}).toString().trim();
 await page.goto("/login");
 await page.getByLabel("Email").fill("employee@example.test");
 await page.getByLabel("Mật khẩu").fill("WorkDemo123!");
 await page.getByRole("button",{name:"Đăng nhập"}).click();
 await expect(page.getByRole("button",{name:"Đăng xuất"})).toBeVisible();
 const tasksResponse=await page.request.get("/api/v1/my-tasks");
 expect(tasksResponse.status()).toBe(200);
 const task=await(await page.request.get(`/api/v1/tasks/${fixtureId}`)).json();
 expect(task).toBeTruthy();
 const before=await(await page.request.get(`/api/v1/tasks/${task.id}`)).json();
 await page.getByRole("button",{name:"Task của tôi",exact:true}).click();
 let taskPage=1;
 while(!(await(await page.request.get(`/api/v1/my-tasks?page=${taskPage}&page_size=20`)).json()).items.some((item:{id:string})=>item.id===task.id)){
  taskPage++;
  expect(taskPage).toBeLessThan(100);
 }
 for(let current=1;current<taskPage;current++){
  await page.getByRole("button",{name:"Trang sau",exact:true}).click();
  await expect(page.getByText(`Trang ${current+1}`,{exact:true})).toBeVisible();
 }
 await page.getByRole("button",{name:task.title,exact:false}).click();
 await expect(page.getByRole("heading",{name:"Báo cáo hằng ngày"})).toBeVisible();

 const form=page.locator("#daily-update-form");
 await form.getByLabel("Tiến độ báo cáo (%)",{exact:true}).fill("50");
 await form.getByLabel("Công việc đã làm",{exact:true}).fill("Đã khảo sát");
 await form.getByLabel("Mô tả vướng mắc",{exact:true}).fill("Thiếu vật liệu");
 await form.getByLabel("Mức độ",{exact:true}).selectOption("HIGH");
 await form.getByRole("button",{name:"Thêm vào báo cáo",exact:true}).click();
 expect(await(await page.request.get(`/api/v1/blockers?task_id=${task.id}`)).json()).toHaveLength(0);
 await form.getByRole("button",{name:"Xem lại báo cáo",exact:true}).click();
 await form.getByRole("button",{name:"Xác nhận báo cáo",exact:true}).click();
 await expect(form.getByText("Đã xác nhận báo cáo",{exact:true})).toBeVisible();
 const panel=page.locator("section").filter({has:page.getByRole("heading",{name:"Vướng mắc",exact:true})}).last();
 for(const action of ["Ghi nhận","Giải quyết","Mở lại"]){
  const changed=page.waitForResponse(r=>r.request().method()==="PATCH"&&r.url().includes("/api/v1/blockers/"));
  await panel.getByRole("button",{name:action,exact:true}).click();
  expect((await changed).status()).toBe(200);
 }
 await panel.getByRole("button",{name:"Lịch sử vướng mắc",exact:true}).click();
 await expect(panel.locator("ol li")).toHaveCount(4);
 const blockers=await(await page.request.get(`/api/v1/blockers?task_id=${task.id}`)).json();
 expect(blockers).toHaveLength(1);expect(blockers[0].status).toBe("OPEN");
 const after=await(await page.request.get(`/api/v1/tasks/${task.id}`)).json();
 expect(after).toEqual(before);
});
