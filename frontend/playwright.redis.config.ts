import { defineConfig, devices } from "@playwright/test";

import { requireRedisTestTarget } from "./e2e/redis-harness";

const { apiPort, frontendPort } = requireRedisTestTarget(process.env);

export default defineConfig({
  testDir: "./e2e",
  testMatch: "redis-workload-flow.spec.ts",
  workers: 1,
  timeout: 90_000,
  expect: { timeout: 20_000 },
  retries: 0,
  use: { baseURL: `http://127.0.0.1:${frontendPort}`, trace: "retain-on-failure" },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: [
    {
      command: "cd ../backend && uv run --locked python -m app.scripts.redis_e2e_server",
      url: `http://127.0.0.1:${apiPort}/openapi.json`,
      reuseExistingServer: false,
      timeout: 60_000,
    },
    {
      command: `API_ORIGIN=http://127.0.0.1:${apiPort} NEXT_DIST_DIR=.next-redis-e2e corepack pnpm@10 dev --hostname 127.0.0.1 --port ${frontendPort}`,
      url: `http://127.0.0.1:${frontendPort}/login`,
      reuseExistingServer: false,
      timeout: 120_000,
    },
  ],
});
