import { defineRailway, project, service, postgres, redis, image, preserve } from "railway/iac";

export default defineRailway(() => {
  const pg = postgres("postgres");
  const cache = redis("redis");

  // Mirrors the live pgbouncer service variables (verified against
  // `railway variables --service pgbouncer --kv` on 2026-08-15).
  //
  // DB_USER/DB_PASSWORD are pgbouncer's OWN admin/auth credentials, not the
  // application's: with AUTH_TYPE=scram-sha-256 the edoburu image connects to
  // the `railway` database as auth_user=postgres and runs pgbouncer's
  // auth_query to look up each *client's* SCRAM verifier on demand. That is
  // exactly what lets the restricted `crawmatic_app` role authenticate through
  // the pooler without appearing in a static userlist.txt.
  //
  // Do NOT downgrade AUTH_TYPE to `trust`: the pooler would stop verifying
  // client passwords entirely and would connect upstream as postgres
  // regardless of the role the client asked for, silently defeating the
  // per-role RLS separation established by the 2026-08-15 cutover.
  const pgbouncer = service("pgbouncer", {
    source: image("edoburu/pgbouncer:v1.23.1-p3"),
    env: {
      DB_HOST: "${{postgres.RAILWAY_PRIVATE_DOMAIN}}",
      DB_PORT: "5432",
      // auth_user for the auth_query flow described above.
      DB_USER: "postgres",
      // Secret: preserved so the live value is never written to source.
      DB_PASSWORD: preserve(),
      // The auth_query runs against the `railway` database, which forwards
      // whatever user the client connected as (e.g. crawmatic_app).
      DB_NAME: "railway",
      POOL_MODE: "transaction",
      AUTH_TYPE: "scram-sha-256",
      LISTEN_ADDR: "*",
      LISTEN_PORT: "6432",
    },
  });

  // Shared across every app member (mirrors docker-compose.yml's single
  // `env_file: .env` — app_shared.config.Settings is imported by all of
  // them and requires this full set, migrate included).
  const commonEnv = {
    // The live value carries the RESTRICTED `crawmatic_app` role's credentials
    // (RLS cutover, 2026-08-15) pointed at pgbouncer:6432. Composing it from
    // ${{postgres.PGUSER}}/${{postgres.PGPASSWORD}} — as this file used to —
    // would hand every app service the postgres superuser again and silently
    // revert the cutover on the next `railway config apply`, with no visible
    // failure to signal it. The system of record for this credential is the
    // Railway variable itself on each of the five app services (api,
    // scheduler, worker, scrapers, scrapers-browser), so it is preserved here
    // rather than composed.
    //
    // Deliberately NOT covered by this: the `migrate` service's
    // MIGRATION_DATABASE_URL (below) and the separately-set AUTH_DATABASE_URL
    // on the services that have it still use superuser / direct-to-postgres
    // credentials on purpose — DDL and role management cannot run as
    // crawmatic_app or through the transaction pooler.
    DATABASE_URL: preserve(),
    DB_POOL_SIZE: "5",
    DB_MAX_OVERFLOW: "2",
    REDIS_URL: cache.env.REDIS_URL,
    SCRAPYD_HTTP_URLS: "http://${{scrapers.RAILWAY_PRIVATE_DOMAIN}}:6800",
    SCRAPYD_BROWSER_URLS: "http://${{scrapers-browser.RAILWAY_PRIVATE_DOMAIN}}:6800",
    SCRAPYD_USERNAME: "scrapyd",
    SCRAPYD_PASSWORD: { generator: 'secret(24, "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")' },
    JWT_SECRET: { generator: 'secret(48, "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")' },
    JWT_ALGORITHM: "HS256",
    ACCESS_TOKEN_TTL_SECONDS: "900",
    REFRESH_TOKEN_TTL_SECONDS: "2592000",
    STATUS_CACHE_TTL_SECONDS: "30",
    LOGIN_RATE_LIMIT_MAX_ATTEMPTS: "5",
    LOGIN_RATE_LIMIT_WINDOW_SECONDS: "60",
    API_KEY_LAST_USED_THROTTLE_SECONDS: "60",
    ENCRYPTION_PRIMARY_KEY_VERSION: "1",
    ACCESS_RESOLUTION_CACHE_TTL_SECONDS: "30",
    // ENCRYPTION_KEYS is deliberately NOT declared here (must be a real
    // Fernet key, not something the generic secret() generator can
    // produce) — it's set post-apply via `railway variable set` on every
    // app service so the value never lands in source.
  };

  const api = service("api", {
    build: { builder: "DOCKERFILE", dockerfilePath: "apps/api/Dockerfile" },
    env: {
      ...commonEnv,
      API_PORT: "8000",
      INTERNAL_API_BASE_URL: "http://${{RAILWAY_PRIVATE_DOMAIN}}:8000",
    },
  });

  const migrate = service("migrate", {
    build: { builder: "DOCKERFILE", dockerfilePath: "apps/migrate/Dockerfile" },
    deploy: { restartPolicyType: "NEVER" },
    env: {
      ...commonEnv,
      MIGRATION_DATABASE_URL:
        "postgresql+psycopg://${{postgres.PGUSER}}:${{postgres.PGPASSWORD}}@${{postgres.RAILWAY_PRIVATE_DOMAIN}}:5432/${{postgres.PGDATABASE}}",
    },
  });

  const scheduler = service("scheduler", {
    build: { builder: "DOCKERFILE", dockerfilePath: "apps/scheduler/Dockerfile" },
    env: { ...commonEnv },
  });

  const worker = service("worker", {
    build: { builder: "DOCKERFILE", dockerfilePath: "apps/workers/Dockerfile" },
    env: {
      ...commonEnv,
      // Maintenance tasks (partition_create / daily_rollup / retention_drop)
      // require a direct, non-pooled connection as crawmatic_auth. The value
      // was set via `railway variables` on 2026-08-15 (see
      // HANDOVER_READINESS_CYCLE_2026-08-15.md §2); the credential must not
      // land in source, so it is preserved rather than composed here.
      SYSTEM_DATABASE_URL: preserve(),
    },
  });

  const scrapers = service("scrapers", {
    build: { builder: "DOCKERFILE", dockerfilePath: "apps/scrapers/Dockerfile" },
    env: { ...commonEnv },
  });

  const scrapersBrowser = service("scrapers-browser", {
    build: { builder: "DOCKERFILE", dockerfilePath: "apps/scrapers-browser/Dockerfile" },
    env: { ...commonEnv },
  });

  return project("Crawmatic", {
    resources: [pg, cache, pgbouncer, api, migrate, scheduler, worker, scrapers, scrapersBrowser],
  });
});
