import { expect,test } from "@playwright/test";

for(const locale of ["vi","en"] as const) {
 test(`report and read-only status through the same conversation in ${locale}`,async({page})=>{
  await page.goto("/login");
  await page.getByLabel("Email").fill("manager@example.test");
  await page.getByLabel("Mật khẩu").fill("WorkDemo123!");
  await page.getByRole("button",{name:"Đăng nhập"}).click();
  await expect(page.getByRole("button",{name:"Đăng xuất"})).toBeVisible();
  if(locale==="en") await page.getByRole("button",{name:"en",exact:true}).click();
  const name=`Conference 2026 ${locale} ${Date.now()}`;
  const created=await page.request.post("/api/v1/projects",{data:{name},headers:{"Idempotency-Key":crypto.randomUUID()}});
  expect(created.status()).toBe(201);const project=await created.json();
  await page.getByRole("navigation").getByRole("button",{name:locale==="vi"?"Cuộc trò chuyện mới":"New conversation"}).click();
  const composer=page.getByLabel(locale==="vi"?"Nhắn cho Trợ lý AI":"Message the AI Assistant");
  await composer.fill(locale==="vi"?`Tạo báo cáo tuần trước cho dự án "${name}"`:`Generate a report for last week for project "${name}"`);
  await page.getByRole("button",{name:locale==="vi"?"Gửi":"Send",exact:true}).click();
  const card=page.getByRole("region",{name:locale==="vi"?"Báo cáo dự án":"Project report",exact:true});
  await expect(card).toBeVisible();
  const conversationId=new URL(page.url()).searchParams.get("conversation");
  const snapshot=await (await page.request.get(`/api/v1/ai/conversations/${conversationId}`)).json();
  const block=snapshot.messages.flatMap((m:{content_blocks:Array<{kind:string}>})=>m.content_blocks).find((b:{kind:string})=>b.kind==="report");
  expect(block.project_id).toBe(project.id);
  const report=await (await page.request.get(`/api/v1/reports/${block.report_id}`)).json();
  expect(report.publications).toHaveLength(0);
  expect(report.snapshot.snapshot_hash).toBe(block.snapshot_hash);
  await composer.fill(locale==="vi"?`Trạng thái dự án "${name}" có gì cần chú ý?`:`Project status for "${name}"?`);
  await page.getByRole("button",{name:locale==="vi"?"Gửi":"Send",exact:true}).click();
  const status=page.getByRole("region",{name:locale==="vi"?"Trạng thái dự án":"Project status",exact:true});
  await expect(status).toBeVisible();
  await page.reload();await expect(card).toBeVisible();await expect(status).toBeVisible();
  const listed=await (await page.request.get(`/api/v1/reports?project_id=${project.id}`)).json();
  expect(listed.total).toBe(1);
  // Ambiguous and missing names produce clarification and no extra report.
  await page.request.post("/api/v1/projects",{data:{name},headers:{"Idempotency-Key":crypto.randomUUID()}});
  await composer.fill(locale==="vi"?`Tạo báo cáo cho dự án "${name}"`:`Generate a report for project "${name}"`);
  await page.getByRole("button",{name:locale==="vi"?"Gửi":"Send",exact:true}).click();
  await expect(page.getByRole("heading",{name:locale==="vi"?"Cần thêm thông tin":"More information needed"}).last()).toBeVisible();
  const beforeMissing=(await (await page.request.get(`/api/v1/ai/conversations/${conversationId}`)).json()).messages.length;
  await composer.fill(locale==="vi"?`Tạo báo cáo cho dự án "Missing ${Date.now()}"`:`Generate a report for project "Missing ${Date.now()}"`);
  await page.getByRole("button",{name:locale==="vi"?"Gửi":"Send",exact:true}).click();
  await expect.poll(async()=>{
    const current=await (await page.request.get(`/api/v1/ai/conversations/${conversationId}`)).json();
    return current.messages.length>beforeMissing&&current.messages.at(-1)?.content_blocks.some((b:{kind:string})=>b.kind==="question");
  }).toBe(true);
  expect((await (await page.request.get(`/api/v1/reports?project_id=${project.id}`)).json()).total).toBe(1);
  await card.getByRole("link",{name:locale==="vi"?"Mở báo cáo":"Open report"}).click();
  const detail=page.getByRole("article",{name:locale==="vi"?"Chi tiết báo cáo":"Report details"});
  await expect(detail).toBeVisible();
  await expect(detail.getByText(new RegExp(block.report_version_id))).toBeVisible();
  await expect(detail.getByRole("button",{name:locale==="vi"?"Xuất bản chỉ số liệu":"Publish metrics only"})).toHaveCount(0);
  await page.getByRole("button",{name:locale==="vi"?"Mở bản nháp hiện tại":"Open current draft"}).click();
  await expect(detail.getByRole("button",{name:locale==="vi"?"Xuất bản chỉ số liệu":"Publish metrics only"})).toBeVisible();
 });
}

test("Employee cannot route a management report or open its direct link",async({page})=>{
 await page.goto("/login");
 await page.getByLabel("Email").fill("employee@example.test");
 await page.getByLabel("Mật khẩu").fill("WorkDemo123!");
 await page.getByRole("button",{name:"Đăng nhập"}).click();
 await page.getByRole("navigation").getByRole("button",{name:"Cuộc trò chuyện mới"}).click();
 await page.getByLabel("Nhắn cho Trợ lý AI").fill('Tạo báo cáo cho dự án "Conference 2026"');
 await page.getByRole("button",{name:"Gửi",exact:true}).click();
 await expect(page.getByRole("heading",{name:"Khả năng chưa khả dụng"})).toBeVisible();
 await expect(page.getByRole("region",{name:"Báo cáo dự án",exact:true})).toHaveCount(0);
 expect((await page.request.get(`/api/v1/reports/${crypto.randomUUID()}?version_id=${crypto.randomUUID()}`)).status()).toBe(403);
});
