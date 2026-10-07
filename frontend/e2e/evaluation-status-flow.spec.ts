import { execFileSync } from "node:child_process";
import { resolve } from "node:path";
import { expect, test } from "@playwright/test";

for(const locale of ["vi","en"] as const){
 test(`Admin starts frozen evaluation and opens safe measured results (${locale})`,async({page})=>{
  const backend=resolve(process.cwd(),"../backend");
  const dataset=execFileSync("uv",["run","--directory",backend,"python",resolve(process.cwd(),"e2e/fixtures/evaluation-seed.py")],{env:{...process.env,PYTHONPATH:backend,APP_DATABASE_URL:"postgresql+psycopg://work_management:work_management@localhost:5432/work_management_e2e",APP_AI_PROVIDER:"mock"},timeout:60000}).toString().trim();
  await page.goto("/login");await page.getByLabel("Email").fill("admin@example.test");await page.getByLabel("Mật khẩu").fill("WorkDemo123!");await page.getByRole("button",{name:"Đăng nhập"}).click();
  await expect(page.getByRole("button",{name:"Đăng xuất"})).toBeVisible();
  if(locale==="en")await page.getByRole("button",{name:"en",exact:true}).click();
  await page.getByRole("button",{name:locale==="vi"?"Đánh giá AI":"AI evaluation",exact:true}).click();
  await page.getByLabel(locale==="vi"?"ID phiên bản dataset":"Dataset version ID").fill(dataset);
  const posted=page.waitForResponse(r=>r.url().endsWith("/api/v1/evaluations/runs")&&r.request().method()==="POST");
  await page.getByRole("button",{name:locale==="vi"?"Chạy đánh giá":"Run evaluation"}).click();
  const started=await posted;expect(started.status()).toBe(202);const run=await started.json();
  await expect(page.getByText(locale==="vi"?"Gate chưa đạt":"Gate failed",{exact:true})).toBeVisible({timeout:60000});
  await expect(page.getByText(run.dataset_hash,{exact:true})).toBeVisible();
  await expect(page.getByText("2 / 2",{exact:true})).toBeVisible();
  const value=await(await page.request.get(`/api/v1/evaluations/runs/${run.id}`)).json();
  expect(value.failure_kind).toBe("GATE");expect(value.result.hosted_quality).toBe("NOT_RUN");
  expect(JSON.stringify(value)).not.toContain('"narrative":');
  await page.getByLabel(locale==="vi"?"ID lần đánh giá":"Evaluation run ID").fill(run.id);
  await page.getByRole("button",{name:locale==="vi"?"Mở kết quả":"Open results"}).click();
  await expect(page.getByText(run.dataset_hash,{exact:true})).toBeVisible();
 });
}
for(const role of ["manager","employee"]){
 test(`${role} cannot start or view evaluations`,async({page})=>{
  await page.goto("/login");await page.getByLabel("Email").fill(`${role}@example.test`);await page.getByLabel("Mật khẩu").fill("WorkDemo123!");await page.getByRole("button",{name:"Đăng nhập"}).click();
  await expect(page.getByRole("button",{name:"Đăng xuất"})).toBeVisible();
  await expect(page.getByRole("button",{name:"Đánh giá AI",exact:true})).toHaveCount(0);
  const response=await page.request.post("/api/v1/evaluations/runs",{headers:{"Idempotency-Key":crypto.randomUUID()},data:{dataset_version_id:crypto.randomUUID()}});
  expect(response.status()).toBe(403);
  expect((await page.request.get(`/api/v1/evaluations/runs/${crypto.randomUUID()}`)).status()).toBe(403);
 });
}
