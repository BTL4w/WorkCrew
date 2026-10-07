import {expect,test} from "@playwright/test";

for(const locale of ["vi","en"] as const){
 test(`verified outcome history and distinct review rates in ${locale}`,async({page})=>{
  test.skip(process.env.APP_AI_PROVIDER==="disabled","requires deterministic mock review");
  await page.goto("/login");
  await page.getByLabel("Email").fill("manager@example.test");
  await page.getByLabel("Mật khẩu").fill("WorkDemo123!");
  await page.getByRole("button",{name:"Đăng nhập"}).click();
  await expect(page.getByRole("button",{name:"Đăng xuất"})).toBeVisible();
  if(locale==="en")await page.getByRole("button",{name:"en",exact:true}).click();
  const name=`Outcome ${locale} ${Date.now()}`;
  const projectResponse=await page.request.post("/api/v1/projects",{data:{name},headers:{"Idempotency-Key":crypto.randomUUID()}});
  expect(projectResponse.status()).toBe(201);const project=await projectResponse.json();
  const weekResponse=await page.request.post(`/api/v1/projects/${project.id}/weeks`,{data:{week_number:1,start_date:"2026-10-05",end_date:"2026-10-11",objective:"Collect feedback"},headers:{"Idempotency-Key":crypto.randomUUID()}});
  expect(weekResponse.status()).toBe(201);const week=await weekResponse.json();
  const taskResponse=await page.request.post("/api/v1/tasks",{data:{project_id:project.id,project_week_id:week.id,title:"Collect customer feedback",estimated_effort_hours:4},headers:{"Idempotency-Key":crypto.randomUUID()}});
  expect(taskResponse.status()).toBe(201);const task=await taskResponse.json();
  await page.reload();await page.getByRole("button",{name,exact:false}).click();
  await page.getByRole("tab",{name:locale==="vi"?"Báo cáo":"Reports",exact:true}).click();
  await page.getByRole("button",{name:locale==="vi"?"Tạo báo cáo":"Create report",exact:true}).click();
  const created=page.waitForResponse(r=>r.url().endsWith("/api/v1/reports")&&r.request().method()==="POST");
  await page.getByRole("button",{name:locale==="vi"?"Tạo báo cáo số liệu":"Generate report",exact:true}).click();
  const initial=await(await created).json();
  await expect(page.getByRole("button",{name:locale==="vi"?"Duyệt và xuất bản":"Review and publish"})).toBeEnabled();
  const publication=page.waitForResponse(r=>r.url().endsWith(`/reports/${initial.report.id}/publish`)&&r.request().method()==="POST");
  await page.getByRole("button",{name:locale==="vi"?"Duyệt và xuất bản":"Review and publish"}).click();
  const published=await(await publication).json();
  expect(published.feedback).toHaveLength(1);expect(published.review_rates.reviewed_generation_count).toBe(1);
  await expect(page.getByText(locale==="vi"?"Kết quả thực tế: chưa có dữ liệu":"Actual outcome: unknown",{exact:true})).toBeVisible();
  const key=crypto.randomUUID();const feedbackId=published.feedback[0].id;
  const body={source_type:"TASK_ACTUALS",source_id:task.id,source_version:0};
  const first=await page.request.post(`/api/v1/feedback/${feedbackId}/outcomes`,{data:body,headers:{"Idempotency-Key":key}});
  expect(first.status()).toBe(201);const outcome=await first.json();expect(outcome.state).toBe("UNKNOWN");
  const replay=await page.request.post(`/api/v1/feedback/${feedbackId}/outcomes`,{data:body,headers:{"Idempotency-Key":key}});
  expect(replay.status()).toBe(201);expect((await replay.json()).id).toBe(outcome.id);
  const advisory=await page.request.post("/api/v1/feedback",{data:{report_id:initial.report.id,report_version_id:published.selected_version.id,decision:"REJECT",reason:"Advisory only"},headers:{"Idempotency-Key":crypto.randomUUID()}});expect(advisory.status()).toBe(201);
  const rates=await page.request.get(`/api/v1/feedback/rates?project_id=${project.id}`);
  expect((await rates.json()).accept_percent).toBe("100");
  const projection=await(await page.request.get(`/api/v1/reports/${initial.report.id}`)).json();
  expect(projection.feedback_outcomes).toHaveLength(1);expect(projection.review_rates.reviewed_generation_count).toBe(1);
  await page.reload();await page.getByRole("button",{name,exact:false}).click();
  await page.getByRole("tab",{name:locale==="vi"?"Báo cáo":"Reports",exact:true}).click();
  await expect(page.getByRole("region",{name:locale==="vi"?"Chất lượng review và kết quả thực tế":"Review quality and actual outcomes"})).toBeVisible();
  await expect(page.getByText(/ACCEPT: 100%/)).toBeVisible();
  await expect(page.getByText(new RegExp(task.id)).first()).toBeVisible();
 });
}
