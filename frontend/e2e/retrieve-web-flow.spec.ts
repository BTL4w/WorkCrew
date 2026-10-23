import { execFileSync } from "node:child_process";
import { expect, test, type Page } from "@playwright/test";

type Locale = "vi" | "en";
type Snapshot = { messages: {content_blocks: {kind: string; outcome?: string; sources?: {method: string}[]}[]}[] };
async function login(page: Page, locale: Locale) {
  await page.goto("/login");
  await page.getByLabel("Email").fill("manager@example.test");
  await page.getByLabel("Mật khẩu").fill("WorkDemo123!");
  await page.getByRole("button", {name:"Đăng nhập",exact:true}).click();
  await expect(page.getByRole("button", {name:"Đăng xuất"})).toBeVisible();
  if (locale === "en") await page.getByRole("button", {name:"en",exact:true}).click();
}
async function send(page: Page, locale: Locale, query: string) {
  await page.getByRole("navigation").getByRole("button", {name:locale === "vi" ? "Cuộc trò chuyện mới" : "New conversation"}).click();
  await page.getByLabel(locale === "vi" ? "Nhắn cho Trợ lý AI" : "Message the AI Assistant").fill((locale === "vi" ? "Tìm trên web về " : "Search public web for ") + query);
  await page.getByRole("button", {name:locale === "vi" ? "Gửi" : "Send",exact:true}).click();
  await expect(page).toHaveURL(/[?&]conversation=[0-9a-f-]+/);
}
function control(page: Page, operation: "inspect" | "stop" | "revoke" | "restore") {
  const conversation_id = new URL(page.url()).searchParams.get("conversation");
  return JSON.parse(execFileSync("../backend/.venv/bin/python", ["tests/retrieval_browser_control.py"], {
    cwd:"../backend", input:JSON.stringify({operation,conversation_id}), encoding:"utf8", env:process.env,
  })) as {dispatched: number; sources: number; projects: number; tasks: number; bound_planning: number};
}
async function snapshot(page: Page): Promise<Snapshot> {
  return (await page.request.get(`/api/v1/ai/conversations/${new URL(page.url()).searchParams.get("conversation")}`)).json();
}
for (const locale of ["vi", "en"] as const) {
  test(`web read, invalid synthesis, empty and quota remain distinct in ${locale}`, async ({page}) => {
    await login(page,locale);
    await send(page,locale,"public planning details");
    await expect(page.getByRole("heading", {name:locale === "vi" ? "Bằng chứng chưa đầy đủ" : "Partial evidence",exact:true})).toBeVisible();
    expect((await snapshot(page)).messages.flatMap(m=>m.content_blocks).filter(b=>b.kind==="retrieval_answer").flatMap(b=>b.sources ?? []).some(s=>s.method==="PAGE_CONTENT")).toBe(true);
    await send(page,locale,"public planning invalid summary");
    await expect(page.getByRole("heading", {name:locale === "vi" ? "Các nguồn đã xác minh" : "Verified sources",exact:true})).toBeVisible();
    await expect(page.getByText("Unsupported synthetic claim", {exact:true})).toHaveCount(0);
    await send(page,locale,"public planning no results");
    await expect(page.getByRole("heading", {name:locale === "vi" ? "Không tìm thấy nguồn phù hợp" : "No matching sources found",exact:true})).toBeVisible();
    await send(page,locale,"public planning quota");
    await expect(page.getByText(locale === "vi" ? "Nguồn web đang không khả dụng" : "Web sources are unavailable",{exact:true})).toBeVisible();
  });
  test(`worker stop fences an in-flight source in ${locale}`, async ({page}) => {
    await login(page,locale);
    await send(page,locale,"public planning slow");
    await expect.poll(()=>control(page,"inspect").dispatched,{intervals:[100]}).toBe(1);
    control(page,"stop");
    await expect.poll(()=>control(page,"inspect").sources,{timeout:4000}).toBe(0);
    // Wait for the bounded mock provider to return before checking the late-result fence.
    await page.waitForTimeout(2300);
    expect(control(page,"inspect").sources).toBe(0);
    expect((await snapshot(page)).messages.flatMap(m=>m.content_blocks).filter(b=>b.kind==="retrieval_answer")).toHaveLength(0);
  });
  test(`membership revocation rejects snapshot and SSE reconnect in ${locale}`, async ({page}) => {
    await login(page,locale);
    await send(page,locale,"public planning slow");
    const id=new URL(page.url()).searchParams.get("conversation");
    await expect.poll(()=>control(page,"inspect").dispatched,{intervals:[100]}).toBe(1);
    try {
      control(page,"revoke");
      for (const path of [`/api/v1/ai/conversations/${id}`,`/api/v1/ai/conversations/${id}/events`]) {
        const response=await page.request.get(path);
        expect([401,403,404]).toContain(response.status());
      }
      await page.waitForTimeout(2300);
      expect(control(page,"inspect").sources).toBe(0);
    } finally { control(page,"restore"); }
  });
  test(`research and planning await human approval in ${locale}`, async ({page}) => {
    await login(page,locale);
    // First conversation provides a scoped fixture handle for before/after business counts.
    await send(page,locale,"public planning");
    await expect(page.getByRole("region",{name:locale === "vi" ? "Nghiên cứu web" : "Web research"})).toBeVisible();
    const before=control(page,"inspect");
    await send(page,locale,"public planning"+(locale === "vi" ? " rồi tạo kế hoạch cho hội thảo" : " then create a plan for a workshop"));
    await expect(page.locator(".assistant-planning-draft")).toBeVisible();
    const after=control(page,"inspect");
    expect(after.projects).toBe(before.projects);
    expect(after.tasks).toBe(before.tasks);
    expect(after.sources).toBeGreaterThan(0);
    expect(after.bound_planning).toBe(1);
  });
}
