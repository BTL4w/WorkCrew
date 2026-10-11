import { defineConfig } from "@playwright/test";
import base from "./playwright.config";

// A fresh, separately migrated synthetic database. No reset target is invoked.
export default defineConfig({
  ...base,
  testMatch: "progressive-planning-flow.spec.ts",
  webServer: Array.isArray(base.webServer)
    ? base.webServer.map((server) => ({
        ...server,
        command: server.command
          .replaceAll("work_management_e2e", "work_management_progressive_e2e")
          .replaceAll(
            "uv run",
            "env APP_AI_PROVIDER=mock APP_PROGRESSIVE_PLANNING_ENABLED=true uv run",
          ),
      }))
    : base.webServer,
});
