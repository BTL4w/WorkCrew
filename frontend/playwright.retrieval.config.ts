import { defineConfig, devices } from "@playwright/test";

const databaseUrl = process.env.RETRIEVAL_TEST_DATABASE_URL;
const organizationId = process.env.RETRIEVAL_TEST_ORGANIZATION_ID;
if (!databaseUrl || !organizationId) throw new Error("Dedicated retrieval test database and organization are required");
const database = new URL(databaseUrl);
if (!["127.0.0.1", "localhost"].includes(database.hostname) || database.port !== "55432" ||
  database.pathname !== "/work_management_retrieve_task6_e2e" || !/^[0-9a-f-]{36}$/i.test(organizationId)) {
  throw new Error("Retrieval browser tests require the dedicated local synthetic database");
}
const env: Record<string, string> = {
  LANGSMITH_TRACING: "false", APP_LANGSMITH_TRACING: "false", APP_DATABASE_URL: databaseUrl, APP_ENVIRONMENT: "local", APP_AI_PROVIDER: "mock",
  APP_RETRIEVAL_ENABLED: "true", APP_RETRIEVAL_PROVIDER: "mock", APP_RETRIEVAL_CORE_EXIT_VERIFIED: "true",
  APP_WORKER_ORGANIZATION_IDS: JSON.stringify([organizationId]),
};
export default defineConfig({
  testDir: "./e2e", testMatch: "retrieval-answer-flow.spec.ts", workers: 1, fullyParallel: false,
  timeout: 90000, expect: {timeout: 25000}, retries: 0, reporter: "list",
  use: {baseURL: "http://127.0.0.1:3100", trace: "retain-on-failure"},
  projects: [{name: "chromium", use: {...devices["Desktop Chrome"]}}],
  webServer: [
    {command: "cd ../backend && uv run --locked fastapi run app/main.py --host 127.0.0.1 --port 8100", env, url: "http://127.0.0.1:8100/openapi.json", reuseExistingServer: false, timeout: 120000},
    {command: "cd ../backend && uv run --locked python -m app.worker", env, wait: {stderr: /Worker .* started/}, reuseExistingServer: false, timeout: 120000},
    {command: "corepack pnpm@10 start --hostname 127.0.0.1 --port 3100", env: {API_ORIGIN:"http://127.0.0.1:8100",NEXT_DIST_DIR:".next-retrieval-e2e"}, url:"http://127.0.0.1:3100/login",reuseExistingServer:false,timeout:120000},
  ],
});
