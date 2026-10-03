import { execFileSync } from "node:child_process";
import { resolve } from "node:path";
import { expect,test } from "@playwright/test";

test("Employee confirms a manual report and corrects hours without changing Task status",async({page})=>{
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
 const originalImage=execFileSync("uv",["run","--directory",backend,"python","-c","from PIL import Image; import sys; Image.new('RGB',(2,2),'white').save(sys.stdout.buffer,format='PNG')"]);
 const uploadResponse=page.waitForResponse(response=>response.request().method()==="POST" && response.url().endsWith("/api/v1/evidence"));
 await page.getByLabel("Chọn bằng chứng",{exact:true}).setInputFiles({name:"original-evidence.png",mimeType:"image/png",buffer:originalImage});
 const uploaded=await uploadResponse;expect(uploaded.status()).toBe(201);
 const proof=await uploaded.json();
 await page.getByRole("checkbox",{name:`${proof.evidence_id} · v1`,exact:true}).check();
 await expect(page.getByRole("link",{name:"Tải xuống",exact:true})).toBeVisible();
 await expect(page.getByRole("button",{name:"Xem nguồn bằng chứng",exact:true})).toHaveCount(0);
 await page.getByLabel("Tiến độ báo cáo (%)",{exact:true}).fill("99");
 await page.getByLabel("Giờ đã làm",{exact:true}).fill("3");
 await page.getByLabel("Giờ còn lại",{exact:true}).fill("1");
 await page.getByLabel("Công việc đã làm",{exact:true}).fill(`Manual report ${Date.now()}`);
 await page.getByLabel("Lý do sửa báo cáo",{exact:true}).fill("Cập nhật thực tế");
 const draftResponse=page.waitForResponse(response=>response.request().method()==="POST"&&response.url().endsWith("/api/v1/daily-updates/drafts"));
 await page.getByRole("button",{name:"Xem lại báo cáo",exact:true}).click();
 await expect(page.getByText("Chưa thể đánh giá minh chứng. Báo cáo sẽ được lưu ở trạng thái chưa đánh giá.")).toBeVisible();
 const reportDraft=await(await draftResponse).json();
 execFileSync("uv",["run","--directory",backend,"python",resolve(process.cwd(),"e2e/fixtures/evidence-assessment-seed.py"),reportDraft.id],{env:{...process.env,PYTHONPATH:backend,APP_DATABASE_URL:"postgresql+psycopg://work_management:work_management@localhost:5432/work_management_e2e"}});
 await page.getByRole("button",{name:"Tải lại đánh giá",exact:true}).click();
 await expect(page.getByRole("button",{name:"Vẫn gửi dù có cảnh báo",exact:true})).toBeDisabled();
 await expect(page.getByText("Mức độ hỗ trợ của bằng chứng dưới 70/100.")).toBeVisible();
 await expect(page.getByText("Điểm đánh giá của AI",{exact:true})).toBeVisible();
 await expect(page.getByText("65/100",{exact:true})).toBeVisible();
 await expect(page.getByText("Bằng chứng chưa hỗ trợ đầy đủ nội dung báo cáo.",{exact:true})).toBeVisible();
 await expect(page.getByText("Bổ sung bằng chứng cho nội dung đã báo cáo.",{exact:true})).toBeVisible();
 await page.getByRole("checkbox",{name:"Tôi đã đọc các cảnh báo này và vẫn muốn gửi báo cáo.",exact:true}).check();
 await page.getByRole("button",{name:"Vẫn gửi dù có cảnh báo",exact:true}).click();

 await expect(page.getByRole("status")).toHaveText("Đã xác nhận báo cáo");
 await page.getByRole("button",{name:"Sửa bản báo cáo",exact:true}).first().click();
 await page.getByLabel("Giờ đã làm",{exact:true}).fill("2");
 await page.getByLabel("Lý do sửa báo cáo",{exact:true}).fill("Sửa lại thời gian thực tế");
 await page.getByRole("button",{name:"Xem lại báo cáo",exact:true}).click();
 await page.getByRole("button",{name:"Xác nhận báo cáo",exact:true}).click();
 await expect(page.getByRole("status")).toHaveText("Đã xác nhận báo cáo");
 await expect(page.getByText("Đã được sửa; số giờ này không tính vào tổng.")).toBeVisible();
 const after=await(await page.request.get(`/api/v1/tasks/${task.id}`)).json();
 expect(after).toEqual(before);
 const history=await(await page.request.get(`/api/v1/daily-updates?task_id=${task.id}`)).json();
 expect(history[0].item.spent_hours).toBe("2");
 expect(history[0].item.corrects_observation_id).toBe(history[1].id);
});
