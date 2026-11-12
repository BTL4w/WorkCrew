import { execFileSync } from "node:child_process";
import { resolve } from "node:path";
import { expect, test } from "@playwright/test";

const outage = process.env.REDIS_E2E_OUTAGE === "1";
for (const [index, locale] of (["vi", "en"] as const).entries()) {
  test(`${locale}: login and logout work, then attempts are limited without losing input`, async ({ page }) => {
    const seconds = outage ? Math.floor(Number(process.hrtime.bigint()) / 1_000_000_000) : Math.floor(Date.now()/1000);
    const remaining = 60-seconds%60;
    if (remaining < 15) await new Promise(resolve => setTimeout(resolve,(remaining+1)*1000));
    await page.addInitScript(language => localStorage.setItem("work-management-locale",language),locale);
    await page.goto("/login");
    const email = `bench-0-${index+4}@example.test`;
    await page.getByLabel("Email").fill(email);
    await page.getByLabel(locale === "vi" ? "Mật khẩu" : "Password").fill("RedisBenchmarkOnly-2026!");
    const response = page.waitForResponse(r=>r.url().endsWith("/api/v1/auth/login"));
    await page.getByRole("button",{name:locale === "vi" ? "Đăng nhập" : "Sign in",exact:true}).click();
    expect((await response).status()).toBe(200);
    expect((await page.request.get("/api/v1/projects")).status()).toBe(200);
    expect((await page.request.post("/api/v1/auth/logout")).status()).toBe(204);
    expect((await page.request.get("/api/v1/me")).status()).toBe(401);
    for (let i=0;i<9;i++) expect((await page.request.post("/api/v1/auth/login",{data:{email,password:"incorrect"}})).status()).toBe(401);
    await page.goto("/login");
    await page.getByLabel("Email").fill(email);
    const password = page.getByLabel(locale === "vi" ? "Mật khẩu" : "Password");
    await password.fill("incorrect");
    const limited = page.waitForResponse(r=>r.url().endsWith("/api/v1/auth/login"));
    await page.getByRole("button",{name:locale === "vi" ? "Đăng nhập" : "Sign in",exact:true}).click();
    expect((await limited).status()).toBe(429);
    await expect(page.getByRole("alert").filter({hasText:locale === "vi" ? "Quá nhiều lần thử đăng nhập" : "Too many sign-in attempts"})).toBeVisible();
    await expect(page.getByLabel("Email")).toHaveValue(email);
    await expect(password).toHaveValue("incorrect");
  });
}

test("untrusted forwarded headers cannot bypass the real server IP counter", async ({request})=>{
  test.skip(outage,"shared-IP probe uses real Redis; outage login tested above");
  execFileSync("uv",["run","--locked","python","-m","app.scripts.redis_e2e_server","fill-test-login-ip"],{cwd:resolve(process.cwd(),"../backend"),env:process.env});
  const result = await request.post("/api/v1/auth/login",{data:{email:"spoof-probe@example.test",password:"incorrect"},headers:{"X-Forwarded-For":"8.8.8.8","Forwarded":"for=8.8.4.4"}});
  expect(result.status()).toBe(429);
  expect((await result.json()).error.code).toBe("LOGIN_RATE_LIMITED");
});
