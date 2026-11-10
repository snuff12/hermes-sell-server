import { expect, test, type Page } from "@playwright/test";

async function owner(page: Page) {
  await page.goto("http://127.0.0.1:8876/");
  await page
    .getByLabel("Admin token", { exact: true })
    .fill("local-preview-only");
  await page
    .getByRole("button", { name: "Open workspace", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Services", exact: true }),
  ).toBeVisible();
}

test("seller registration checks a configured API and links to the private workspace", async ({
  page,
}) => {
  await page.setViewportSize({ width: 360, height: 1000 });
  await page.goto("http://127.0.0.1:8879/sell");
  await page
    .getByLabel("Seller API URL", { exact: true })
    .selectOption("http://127.0.0.1:8875");
  await page
    .getByLabel("Owner workspace URL", { exact: true })
    .fill("https://untrusted.example/");
  await page.getByRole("button", { name: "Check seller", exact: true }).click();
  await expect(
    page.getByText("Simulation seller: connected and ready.", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("link", { name: "Open owner workspace", exact: true }),
  ).toHaveCount(0);
  await page
    .getByLabel("Owner workspace URL", { exact: true })
    .fill("http://127.0.0.1:8876");
  await expect(
    page.getByRole("link", { name: "Open owner workspace", exact: true }),
  ).toHaveAttribute("href", "http://127.0.0.1:8876");
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "test-results/seller-registration-360.png",
    fullPage: true,
  });
  await page
    .getByRole("link", { name: "Open owner workspace", exact: true })
    .click();
  await expect(page.getByLabel("Admin token", { exact: true })).toBeVisible();
});

test("a completed purchase can publish and update one simulation rating", async ({
  page,
}) => {
  await owner(page);
  const service = page
    .locator("article")
    .filter({
      has: page.getByRole("heading", { name: "Diff review", exact: true }),
    });
  await expect(
    service.getByLabel("Share usage and settled volume publicly", {
      exact: true,
    }),
  ).not.toBeChecked();
  await service
    .getByRole("button", { name: "List in directory", exact: true })
    .click();
  await expect(
    page.getByText(
      "Listed in the service directory. Buyers can discover this version.",
      { exact: true },
    ),
  ).toBeVisible();
  await page.goto("http://127.0.0.1:8879/");
  await page
    .getByRole("link", { name: "View service", exact: true })
    .first()
    .click();
  const detailUrl = page.url();
  await expect(
    page.getByRole("heading", { name: "Seller-reported usage", exact: true }),
  ).toHaveCount(0);
  await page
    .getByRole("link", { name: "Open storefront", exact: true })
    .click();
  await page
    .getByRole("button", { name: "Connect test session", exact: true })
    .click();
  await page.getByRole("button", { name: "Review quote", exact: true }).click();
  await page
    .getByRole("button", { name: "Approve 0.10 USDC simulation", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Result", exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Rate this service", exact: true })
    .click();
  await page.getByLabel("Rating", { exact: true }).selectOption("4");
  await page
    .getByLabel("Review", { exact: true })
    .fill("Useful simulation findings.");
  await page
    .getByRole("button", { name: "Publish simulation review", exact: true })
    .click();
  await expect(page.getByText(/Your review is published/)).toBeVisible();
  await page.getByLabel("Rating", { exact: true }).selectOption("5");
  await page
    .getByLabel("Review", { exact: true })
    .fill("Updated simulation feedback.");
  await page
    .getByRole("button", { name: "Publish simulation review", exact: true })
    .click();
  await expect(page.getByText(/Your review is published/)).toBeVisible();
  await page.goto(detailUrl);
  await expect(page.getByText("★ 5 · 1 review", { exact: true })).toBeVisible();
  await expect(
    page.getByText("Updated simulation feedback.", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("Useful simulation findings.", { exact: true }),
  ).toHaveCount(0);
  await page.setViewportSize({ width: 360, height: 1000 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "test-results/review-360.png",
    fullPage: true,
  });
  await owner(page);
  await page.getByRole("button", { name: "Get started", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Register as a seller", exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Open my services", exact: true })
    .click();
  const row = page
    .locator("article")
    .filter({
      has: page.getByRole("heading", { name: "Diff review", exact: true }),
    });
  await row
    .getByLabel("Share usage and settled volume publicly", { exact: true })
    .check();
  await row
    .getByRole("button", { name: "List in directory", exact: true })
    .click();
  await expect(
    page.getByText(
      "Listed in the service directory. Buyers can discover this version.",
      { exact: true },
    ),
  ).toBeVisible();
  await page.getByRole("button", { name: "Analytics", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Usage and payments", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("Simulated settled volume", { exact: true }),
  ).toBeVisible();
  await page.getByLabel("Analytics period", { exact: true }).selectOption("7d");
  await expect(page.locator(".usage-bar-slot")).toHaveCount(7);
  await page
    .getByLabel("Analytics period", { exact: true })
    .selectOption("all");
  await expect(page.locator(".usage-bar-slot")).toHaveCount(30);
  await expect(
    page
      .locator(".metric-card")
      .filter({ hasText: "Executions started" })
      .locator("strong"),
  ).not.toHaveText("0");
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "test-results/analytics-360.png",
    fullPage: true,
  });
  await page.goto(detailUrl);
  await expect(
    page.getByRole("heading", { name: "Seller-reported usage", exact: true }),
  ).toBeVisible();
  await expect(page.getByText(/Simulation totals/)).toBeVisible();
});

test("buyer analytics displays exact large totals and cross-seller spending without payment actions", async ({
  page,
}) => {
  let posts = 0;
  await page.route("**/purchases", async (route) =>
    route.fulfill({ response: await page.request.get("/") }),
  );
  await page.route("**/ui-config", (route) =>
    route.fulfill({
      json: { role: "buyer", mode: "testnet", features: ["analytics"] },
    }),
  );
  await page.route("**/buyer/**", (route) => {
    if (route.request().method() === "POST") posts++;
    return route.fulfill({
      json: route.request().url().endsWith("/analytics")
        ? {
            summary: {
              settled_amount: "9007199254740993",
              pending_amount: "200000",
              paid_requests: 2,
              budget_committed: "9007199254940993",
              budget_available: "300000",
            },
            sellers: [
              {
                origin: "https://one.example",
                paid_requests: 1,
                settled_amount: "100000",
              },
              {
                origin: "https://two.example",
                paid_requests: 1,
                settled_amount: "9007199254640993",
              },
            ],
          }
        : { purchases: [] },
    });
  });
  await page.setViewportSize({ width: 360, height: 1000 });
  await page.goto("/purchases");
  await expect(
    page.getByText("9,007,199,254.740993 USDC", { exact: true }),
  ).toBeVisible();
  await page.getByText("Spending by seller", { exact: true }).click();
  await expect(
    page.getByText("https://two.example", { exact: true }),
  ).toBeVisible();
  expect(posts).toBe(0);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
});
