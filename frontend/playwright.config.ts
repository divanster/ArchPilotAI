import { defineConfig } from "@playwright/test";

const BACKEND_PORT = 8100;
const FRONTEND_PORT = 5183;
const LOCAL_HOSTS = ["localhost", "127.0.0.1", "::1"];

// Safety guard: E2E tests write real data, so only a local *_test database is allowed.
const dbName = process.env.ARCHPILOT_DB_NAME ?? "";
const dbHost = process.env.ARCHPILOT_DB_HOST ?? "";
if (!dbName.endsWith("_test") || !LOCAL_HOSTS.includes(dbHost)) {
  throw new Error(
    "E2E requires ARCHPILOT_DB_HOST=localhost and ARCHPILOT_DB_NAME ending in '_test' " +
      `(got host='${dbHost}', name='${dbName}'). Refusing to run.`,
  );
}

const frontendOrigin = `http://localhost:${FRONTEND_PORT}`;
const backendOrigin = `http://localhost:${BACKEND_PORT}`;
const isCI = !!process.env.CI;

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  retries: isCI ? 1 : 0,
  forbidOnly: isCI,
  reporter: isCI ? [["list"], ["html", { open: "never" }]] : [["list"]],
  use: {
    baseURL: frontendOrigin,
    channel: process.env.E2E_BROWSER_CHANNEL || undefined,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  webServer: [
    {
      // Real backend against the isolated test database; migrations run first.
      command: `python -m alembic upgrade head && python -m uvicorn app.main:app --port ${BACKEND_PORT}`,
      cwd: "../backend",
      url: `${backendOrigin}/health`,
      reuseExistingServer: false,
      timeout: 60_000,
      env: {
        ...(process.env as Record<string, string>),
        ARCHPILOT_ENVIRONMENT: "test",
        ARCHPILOT_LLM_PROVIDER: "fake",
        ARCHPILOT_EMBEDDING_PROVIDER: "fake",
        ARCHPILOT_AI_MIN_INTERVAL_SECONDS: "0",
        ARCHPILOT_CORS_ORIGINS: JSON.stringify([frontendOrigin]),
      },
    },
    {
      command: `npm run dev -- --port ${FRONTEND_PORT} --strictPort`,
      url: frontendOrigin,
      reuseExistingServer: false,
      timeout: 60_000,
      env: {
        ...(process.env as Record<string, string>),
        VITE_API_URL: backendOrigin,
      },
    },
  ],
});
