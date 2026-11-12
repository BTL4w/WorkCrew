export function requireRedisTestTarget(env: Record<string, string | undefined>) {
  const token = env.REDIS_BENCHMARK_INSTANCE_TOKEN ?? "";
  try {
    const database = new URL(env.REDIS_E2E_DATABASE_URL ?? "");
    if (database.protocol !== "postgresql+psycopg:" ||
        !["127.0.0.1", "localhost", "[::1]"].includes(database.hostname) ||
        !database.port || database.port === "5432" || database.search || database.hash ||
        !/^[a-f0-9]{32}$/.test(token) ||
        database.pathname !== `/wm_redis_bench_${token.slice(0, 12)}`) {
      throw new Error("invalid target");
    }
    const redis = new URL(env.REDIS_TEST_URL ?? "");
    if (redis.protocol !== "redis:" ||
        !["127.0.0.1", "localhost", "[::1]"].includes(redis.hostname) ||
        !redis.port || redis.search || redis.hash) throw new Error("invalid Redis target");
  } catch {
    throw new Error("DEDICATED_DATABASE_REQUIRED");
  }
  return { apiPort: 8117, frontendPort: 3117 };
}
