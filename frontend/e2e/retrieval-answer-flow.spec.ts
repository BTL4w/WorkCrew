import { expect, test } from "@playwright/test";

for (const locale of ["vi", "en"] as const) {
  test(`public web citations survive refresh and vague planning does not search in ${locale}`, async ({page}, testInfo) => {
    const external: string[] = [];
    page.on("request", request => {
      const host = new URL(request.url()).hostname;
      if (!["127.0.0.1", "localhost"].includes(host)) external.push(request.url());
    });
    await page.goto("/login");
    await page.getByLabel("Email").fill("manager@example.test");
    await page.getByLabel("Mật khẩu").fill("WorkDemo123!");
    await page.getByRole("button", {name:"Đăng nhập"}).click();
    await expect(page.getByRole("button", {name:"Đăng xuất"})).toBeVisible();
    if (locale === "en") await page.getByRole("button",{name:"en",exact:true}).click();
    await page.getByRole("navigation").getByRole("button",{name:locale === "vi" ? "Cuộc trò chuyện mới" : "New conversation"}).click();
    const composer = page.getByLabel(locale === "vi" ? "Nhắn cho Trợ lý AI" : "Message the AI Assistant");
    await composer.fill(locale === "vi" ? "Tìm trên web về public planning" : "Search public web for public planning");
    await page.getByRole("button",{name:locale === "vi" ? "Gửi" : "Send",exact:true}).click();
    const card = page.getByRole("region",{name:locale === "vi" ? "Nghiên cứu web" : "Web research"});
    await expect(card).toBeVisible();
    await expect(card.getByRole("link", {name:/Public planning|Synthetic planning/}).first()).toHaveAttribute("rel","noopener noreferrer");
    await expect(card.getByText(locale === "vi" ? "Chưa rõ ngày xuất bản" : "Publication date unknown").first()).toBeVisible();
    const conversation = new URL(page.url()).searchParams.get("conversation");
    const before = await (await page.request.get(`/api/v1/ai/conversations/${conversation}`)).json();
    const sources = before.messages.flatMap((message: {content_blocks: {kind:string;sources?:unknown[]}[]}) => message.content_blocks).filter((block: {kind:string}) => block.kind === "retrieval_answer");
    expect(sources).toHaveLength(1);
    await page.reload();
    await expect(card).toBeVisible();
    const after = await (await page.request.get(`/api/v1/ai/conversations/${conversation}`)).json();
    expect(after.messages).toEqual(before.messages);
    await page.screenshot({path: testInfo.outputPath("retrieval-answer.png"), fullPage: true});
    // A missing goal uses the ordinary clarification path and creates no second web card.
    await composer.fill(locale === "vi" ? "Tạo kế hoạch cho tôi" : "Create a plan for me");
    await page.getByRole("button",{name:locale === "vi" ? "Gửi" : "Send",exact:true}).click();
    await expect.poll(async () => {
      const current = await (await page.request.get(`/api/v1/ai/conversations/${conversation}`)).json();
      return current.messages.at(-1)?.content_blocks.some((block: {kind:string}) => block.kind === "question" || block.kind === "text");
    }).toBe(true);
    await expect(card).toHaveCount(1);
    expect(external).toEqual([]);
  });
}
