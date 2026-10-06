import {expect,test} from "@playwright/test";

for(const locale of ["vi","en"] as const){
 test(`exact narrative edit review and rejection in ${locale}`,async({page})=>{
  test.skip(process.env.APP_AI_PROVIDER==="disabled","review needs the deterministic mock provider");
  await page.goto("/login");await page.getByLabel("Email").fill("manager@example.test");await page.getByLabel("Mật khẩu").fill("WorkDemo123!");await page.getByRole("button",{name:"Đăng nhập"}).click();await expect(page.getByRole("button",{name:"Đăng xuất"})).toBeVisible();
  if(locale==="en")await page.getByRole("button",{name:"en",exact:true}).click();
  const name=`Review ${locale} ${Date.now()}`;
  const create=await page.request.post("/api/v1/projects",{data:{name},headers:{"Idempotency-Key":crypto.randomUUID()}});expect(create.status()).toBe(201);const project=await create.json();
  await page.reload();await page.getByRole("button",{name,exact:false}).click();await page.getByRole("tab",{name:locale==="vi"?"Báo cáo":"Reports",exact:true}).click();
  async function createReport(){
   await page.getByRole("button",{name:locale==="vi"?"Tạo báo cáo":"Create report",exact:true}).click();
   const response=page.waitForResponse(r=>r.url().endsWith("/api/v1/reports")&&r.request().method()==="POST");
   await page.getByRole("button",{name:locale==="vi"?"Tạo báo cáo số liệu":"Generate report",exact:true}).click();
   const result=await(await response).json();
   await expect(page.getByRole("button",{name:locale==="vi"?"Duyệt và xuất bản":"Review and publish"})).toBeEnabled();
   return result;
  }
  const initial=await createReport();
  const original=await(await page.request.get(`/api/v1/reports/${initial.report.id}`)).json();
  // Existing metrics publication must remain immutable through edit/verification.
  await page.getByRole("button",{name:locale==="vi"?"Xuất bản chỉ số liệu":"Publish metrics only"}).click();await expect(page.getByText(locale==="vi"?"Bản xuất bản hiện tại":"Current publication",{exact:true})).toBeVisible();
  const beforeEdit=await(await page.request.get(`/api/v1/reports/${initial.report.id}`)).json();
  await page.getByRole("button",{name:locale==="vi"?"Sửa nhận xét":"Edit narrative"}).click();
  await page.getByRole("button",{name:locale==="vi"?"Thêm giới hạn":"Add limitation"}).click();
  await page.getByRole("textbox",{name:new RegExp(locale==="vi"?"Nhận xét · note_":"Narrative · note_")}).fill(locale==="vi"?"Chỉ bao gồm dữ liệu đã chụp.":"Only captured data is included.");
  const editResponse=page.waitForResponse(r=>r.url().endsWith(`/reports/${initial.report.id}/versions`)&&r.request().method()==="POST");
  await page.getByRole("button",{name:locale==="vi"?"Lưu và kiểm chứng":"Save and verify"}).click();
  const edited=await(await editResponse).json();expect(edited.verification_state).toBe("PENDING");expect(edited.selected_version.origin).toBe("AI_EDITED");expect(edited.selected_version.generation_id).toBe(original.generation_id);expect(edited.snapshot).toEqual(initial.snapshot);
  // A second tab's stale version cannot publish or leave feedback.
  const stale=await page.request.post(`/api/v1/reports/${initial.report.id}/publish`,{data:{mode:"REVIEWED_NARRATIVE",report_version_id:original.selected_version.id,snapshot_hash:initial.snapshot.snapshot_hash},headers:{"Idempotency-Key":crypto.randomUUID(),"If-Match":`"${beforeEdit.report.version}"`}});expect(stale.status()).toBe(412);
  await expect(page.getByRole("button",{name:locale==="vi"?"Duyệt và xuất bản":"Review and publish"})).toBeEnabled();
  const publishResponse=page.waitForResponse(r=>r.url().endsWith(`/reports/${initial.report.id}/publish`)&&r.request().method()==="POST");
  await page.getByRole("button",{name:locale==="vi"?"Duyệt và xuất bản":"Review and publish"}).click();
  expect((await publishResponse).status()).toBe(201);
  await expect(page.getByRole("button",{name:locale==="vi"?"Duyệt và xuất bản":"Review and publish"})).toBeDisabled();
  const published=await(await page.request.get(`/api/v1/reports/${initial.report.id}`)).json();expect(published.review_state).toBe("ACCEPTED");expect(published.publications).toHaveLength(2);expect(published.published_versions.find((v:{id:string})=>v.id===edited.selected_version.id).narrative).toEqual(edited.selected_version.narrative);
  await page.getByRole("button",{name:locale==="vi"?"Xem bản đã xuất bản":"View published version"}).first().click();
  await expect(page.getByText(locale==="vi"?"Chỉ bao gồm dữ liệu đã chụp.":"Only captured data is included.",{exact:true})).toHaveCount(2);
  await page.getByRole("button",{name:locale==="vi"?"Đóng bản đã xuất bản":"Close published version"}).click();
  await page.getByLabel(locale==="vi"?"Lý do góp ý":"Feedback reason").fill(locale==="vi"?"Nhận xét hữu ích.":"Useful narrative.");await page.getByRole("button",{name:locale==="vi"?"Gửi góp ý":"Send feedback"}).click();await expect(page.getByText(locale==="vi"?"Đã lưu góp ý":"Feedback saved",{exact:true})).toBeVisible();
  const second=await createReport();
  await page.getByLabel(locale==="vi"?"Lý do từ chối":"Reason for rejection").fill(locale==="vi"?"Cần thêm ngữ cảnh.":"Needs more context.");const rejectResponse=page.waitForResponse(r=>r.url().endsWith(`/reports/${second.report.id}/review-decisions`)&&r.request().method()==="POST");await page.getByRole("button",{name:locale==="vi"?"Từ chối nhận xét":"Reject narrative"}).click();expect((await rejectResponse).status()).toBe(201);
  await expect(page.getByRole("button",{name:locale==="vi"?"Duyệt và xuất bản":"Review and publish"})).toBeDisabled();
  const rejected=await(await page.request.get(`/api/v1/reports/${second.report.id}`)).json();expect(rejected.review_state).toBe("REJECTED");expect(rejected.publications).toHaveLength(0);expect(rejected.report.project_id).toBe(project.id);
 });
}
