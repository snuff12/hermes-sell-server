import { defineConfig } from "@playwright/test";
export default defineConfig({
  testDir: "./e2e",
  timeout: 25000,
  workers: 1,
  retries: 0,
  use: {
    baseURL: "http://127.0.0.1:8875",
    headless: true,
    launchOptions: {
      executablePath:
        process.env.CHROME_PATH ||
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    },
    screenshot: "only-on-failure",
  },
  webServer: {
    command: "../.venv/bin/python ../scripts/preview_test_server.py",
    url: "http://127.0.0.1:8875/health/ready",
    reuseExistingServer: false,
    timeout: 20000,
  },
});
