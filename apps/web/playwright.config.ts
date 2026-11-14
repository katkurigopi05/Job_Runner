import { defineConfig, devices } from "@playwright/test";

const API_PORT = Number(process.env.E2E_API_PORT ?? 18321);
const WEB_PORT = Number(process.env.E2E_WEB_PORT ?? 13321);
const BASE_URL = `http://127.0.0.1:${WEB_PORT}`;
const WEB_COMMAND = process.env.E2E_MODE === "start" ? "node e2e/serve.mjs start" : "node e2e/serve.mjs dev";

export default defineConfig({
  testDir: "./e2e",
  outputDir: `test-results/run-${Date.now()}-${process.pid}`,
  timeout: 45_000,
  expect: {
    timeout: 10_000,
  },
  fullyParallel: false,
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: BASE_URL,
    // Trace instrumentation injects scripts into srcdoc and produces Chromium
    // errors when the résumé sandbox correctly blocks them. Keep the sandbox
    // and zero-error assertions; collect failure screenshots without tracing.
    trace: "off",
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
  webServer: [
    {
      command: "node e2e/mock-api.mjs",
      url: `http://127.0.0.1:${API_PORT}/health`,
      reuseExistingServer: false,
      env: {
        E2E_API_PORT: String(API_PORT),
      },
    },
    {
      command: WEB_COMMAND,
      url: `${BASE_URL}/setup`,
      timeout: 240_000,
      gracefulShutdown: { signal: "SIGTERM", timeout: 5000 },
      reuseExistingServer: false,
      env: {
        JOBRUNNER_WEB_HOST: "127.0.0.1",
        JOBRUNNER_WEB_PORT: String(WEB_PORT),
        JOBRUNNER_API: `http://127.0.0.1:${API_PORT}`,
      },
    },
  ],
});
