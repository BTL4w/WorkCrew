import { expect, test } from "@playwright/test";

for (const locale of ["vi", "en"] as const) {
  test(`report narrative lifecycle and explicit retry in ${locale}`, async ({page}) => {
    await page.goto("/login");
    await page.getByLabel("Email").fill("manager@example.test");
    await page.getByLabel("Mật khẩu").fill("WorkDemo123!");
    await page.getByRole("button",{name:"Đăng nhập"}).click();
    await expect(page.getByRole("button",{name:"Đăng xuất"})).toBeVisible();
    if(locale==="en") await page.getByRole("button",{name:"en",exact:true}).click();
    const name=`Narrative ${locale} ${Date.now()}`;
    const created=await page.request.post("/api/v1/projects",{data:{name},headers:{"Idempotency-Key":crypto.randomUUID()}});
    expect(created.status()).toBe(201);
    const project=await created.json();
    await page.reload();
    await page.getByRole("button",{name,exact:false}).click();
    await page.getByRole("tab",{name:locale==="vi"?"Báo cáo":"Reports",exact:true}).click();
    await page.getByRole("button",{name:locale==="vi"?"Tạo báo cáo":"Create report",exact:true}).click();
    await expect(page.getByRole("checkbox",{name:locale==="vi"?"Yêu cầu diễn giải bằng AI":"Request an AI narrative"})).toBeChecked();
    // Observe the actual fast create response, independent of worker scheduling speed.
    const response=page.waitForResponse(r=>r.url().endsWith("/api/v1/reports")&&r.request().method()==="POST");
    await page.getByRole("button",{name:locale==="vi"?"Tạo báo cáo số liệu":"Generate report",exact:true}).click();
    const first=await (await response).json();
    expect(first.generation_state).toBe("QUEUED");
    expect(first.selected_version.origin).toBe("METRICS_ONLY");
    const narrative=page.getByRole("region",{name:locale==="vi"?"Bản nháp báo cáo AI":"AI report draft"});
    const disabled=process.env.APP_AI_PROVIDER==="disabled";
    await expect(narrative.getByRole("status")).toHaveText(disabled
      ? locale==="vi"?"Không có diễn giải AI. Có thể xuất bản số liệu hoặc chủ động thử lại.":"AI narrative is unavailable. Publish metrics or retry explicitly."
      : locale==="vi"?"Bản nháp AI đã sẵn sàng, chờ Quản lý xem xét.":"AI draft is ready for Manager review.");
    const before=await (await page.request.get(`/api/v1/reports/${first.report.id}`)).json();
    expect(before.snapshot).toEqual(first.snapshot);
    expect(before.publications).toHaveLength(0);
    if(disabled) {
      const retryResponse=page.waitForResponse(r=>r.url().endsWith(`/reports/${first.report.id}/generate`)&&r.request().method()==="POST");
      await narrative.getByRole("button",{name:locale==="vi"?"Tạo bản nháp AI":"Generate AI draft"}).click();
      const retried=await (await retryResponse).json();
      expect(retried.generation_state).toBe("QUEUED");
      expect(retried.generation_id).not.toBe(first.generation_id);
      expect(retried.snapshot).toEqual(first.snapshot);
      await expect(narrative.getByRole("status")).toHaveText(locale==="vi"?"Không có diễn giải AI. Có thể xuất bản số liệu hoặc chủ động thử lại.":"AI narrative is unavailable. Publish metrics or retry explicitly.");
    } else {
      expect(before.selected_version.origin).toBe("AI_PROPOSED");
      await expect(narrative.getByText("tasks.status.done_count",{exact:true})).toBeVisible();
      await expect(narrative.getByText(locale==="vi"?"Bản nháp đã được kiểm chứng, chờ Quản lý xem xét. Xuất bản số liệu giữ riêng phần diễn giải.":"Verified draft awaiting Manager review. Publishing metrics keeps the narrative separate.")).toBeVisible();
    }
    await page.getByRole("button",{name:locale==="vi"?"Xuất bản chỉ số liệu":"Publish metrics only",exact:true}).click();
    await expect(page.getByText(locale==="vi"?"Bản xuất bản hiện tại":"Current publication",{exact:true})).toBeVisible();
    const after=await (await page.request.get(`/api/v1/reports/${first.report.id}`)).json();
    expect(after.publications[0].report_version_id).toBe(first.selected_version.id);
    expect(after.snapshot).toEqual(first.snapshot);
    expect(after.report.project_id).toBe(project.id);
  });
}
