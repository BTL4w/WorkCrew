// @vitest-environment node
import { describe, expect, it } from "vitest";

import { requireRedisTestTarget } from "../../../e2e/redis-harness";

describe("Redis browser target guard", () => {
  it("rejects normal application databases before starting any server", () => {
    expect(() => requireRedisTestTarget({
      REDIS_E2E_DATABASE_URL: "postgresql+psycopg://user:secret@127.0.0.1:5432/work_management",
      REDIS_BENCHMARK_INSTANCE_TOKEN: "a".repeat(32),
      REDIS_TEST_URL: "redis://workcrew@127.0.0.1:6379/0",
    })).toThrow("DEDICATED_DATABASE_REQUIRED");
  });

  it("binds the database name to the task token without exposing credentials", () => {
    const target = requireRedisTestTarget({
      REDIS_E2E_DATABASE_URL: "postgresql+psycopg://user:secret@127.0.0.1:55435/wm_redis_bench_aaaaaaaaaaaa",
      REDIS_BENCHMARK_INSTANCE_TOKEN: "a".repeat(32),
      REDIS_TEST_URL: "redis://workcrew@127.0.0.1:55436/0",
    });
    expect(target).toEqual({ apiPort: 8117, frontendPort: 3117 });
  });
});
