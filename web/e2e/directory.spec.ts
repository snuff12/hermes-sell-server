import { expect, test } from "@playwright/test";

test("two sellers publish to one directory and a browser buys from the selected seller", async ({
  page,
}) => {
  const admissions: string[] = [];
  page.on("request", (request) => {
    if (request.method() === "POST" && /\/v1\/orders\//.test(request.url()))
      admissions.push(request.url());
  });
  async function owner(port: number) {
    await page.goto(`http://127.0.0.1:${port}/`);
    await page
      .getByLabel("Admin token", { exact: true })
      .fill("local-preview-only");
    await page
      .getByRole("button", { name: "Open workspace", exact: true })
      .click();
  }
  await owner(8876);
  const first = page
    .locator("article")
    .filter({
      has: page.getByRole("heading", { name: "Diff review", exact: true }),
    });
  await first
    .getByRole("button", { name: "List in directory", exact: true })
    .click();
  await expect(
    page.getByText(
      "Listed in the service directory. Buyers can discover this version.",
      { exact: true },
    ),
  ).toBeVisible();
  await owner(8878);
  await page
    .getByRole("button", { name: "List in directory", exact: true })
    .click();
  await expect(
    page.getByText(
      "Listed in the service directory. Buyers can discover this version.",
      { exact: true },
    ),
  ).toBeVisible();
  await page.goto("http://127.0.0.1:8879/");
  await expect(
    page.getByRole("heading", {
      name: "Services from the community",
      exact: true,
    }),
  ).toBeVisible();
  await expect(page.locator("article")).toHaveCount(2);
  await page.getByLabel("Search services", { exact: true }).fill("River");
  await expect(page.locator("article")).toHaveCount(1);
  await page.getByRole("link", { name: "View service", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "River code review", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("http://127.0.0.1:8877", { exact: true }),
  ).toBeVisible();
  await page
    .getByRole("link", { name: "Open storefront", exact: true })
    .click();
  await expect(page).toHaveURL("http://127.0.0.1:8877/s/diff-review");
  await page
    .getByRole("button", { name: "Connect test session", exact: true })
    .click();
  await page.getByRole("button", { name: "Review quote", exact: true }).click();
  await page
    .getByRole("button", { name: "Approve 0.20 USDC simulation", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Result", exact: true }),
  ).toBeVisible();
  await page.reload();
  await page
    .getByRole("button", { name: "Reconnect and restore order", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Result", exact: true }),
  ).toBeVisible();
  expect(admissions).toHaveLength(1);
  expect(admissions[0]).toContain("127.0.0.1:8877/");
  await owner(8878);
  await page.getByRole("button", { name: "Unlist", exact: true }).click();
  await page.goto("http://127.0.0.1:8879/");
  await page.getByLabel("Search services", { exact: true }).fill("River");
  await expect(
    page.getByRole("heading", { name: "No matching services", exact: true }),
  ).toBeVisible();
});

test("directory fits 360px and preserves seller and payment-mode labels", async ({
  page,
}) => {
  await page.setViewportSize({ width: 360, height: 1000 });
  await page.goto("http://127.0.0.1:8879/");
  await expect(
    page.getByRole("heading", {
      name: "Services from the community",
      exact: true,
    }),
  ).toBeVisible();
  await page
    .getByLabel("Payment mode", { exact: true })
    .selectOption("testnet");
  await expect(
    page.getByRole("heading", { name: "No matching services", exact: true }),
  ).toBeVisible();
  await page
    .getByLabel("Payment mode", { exact: true })
    .selectOption("simulation");
  await expect(
    page.getByText("http://127.0.0.1:8875", { exact: true }),
  ).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "test-results/directory-360.png",
    fullPage: true,
  });
});

test("purchase history shows results and original sellers without payment submission", async ({
  page,
}) => {
  await page.route("**/purchases", async (route) =>
    route.fulfill({ response: await page.request.get("/") }),
  );
  const records = [1, 2].map((number) => ({
    purchase_id: "saved-" + number,
    origin: "https://seller" + number + ".example",
    status: number === 1 ? "settled" : "unknown",
    approval_url: "/approve/saved-" + number,
    order: {
      amount: "100000",
      payment: number === 1 ? "settled" : "unknown",
      service_contract: { name: "Review " + number },
      result: number === 1 ? { summary: "Purchased finding" } : undefined,
      receipt: { transaction: "0x" + "a".repeat(64) },
    },
  }));
  let submits = 0;
  await page.route("**/ui-config", (route) =>
    route.fulfill({
      json: {
        role: "buyer",
        mode: "testnet",
        directory_origin: "http://127.0.0.1:8879",
      },
    }),
  );
  await page.route("**/buyer/**", (route) => {
    if (route.request().method() === "POST") submits++;
    const path = new URL(route.request().url()).pathname;
    return route.fulfill({
      json: path.endsWith("/status") ? records[0] : { purchases: records },
    });
  });
  await page.goto("/purchases");
  await expect(
    page.getByRole("heading", { name: "Your purchases", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("https://seller1.example", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("https://seller2.example", { exact: true }),
  ).toBeVisible();
  await expect(page.getByText(/Purchased finding/)).toBeVisible();
  await page
    .getByRole("button", { name: "Refresh status", exact: true })
    .first()
    .click();
  expect(submits).toBe(0);
});

test("approval startup sends the original purchase context before fetching wallet state", async ({
  page,
}) => {
  await page.route("**/approve/original-purchase", async (route) =>
    route.fulfill({ response: await page.request.get("/") }),
  );
  let context = "";
  await page.route("**/ui-config", (route) => {
    context = route.request().headers()["x-purchase-id"];
    return route.fulfill({
      json: {
        role: "buyer",
        mode: "testnet",
        seller_origin: "https://seller2.example",
        purchase_id: context,
      },
    });
  });
  await page.route("**/buyer/**", (route) =>
    route.fulfill({ json: { connected: false } }),
  );
  await page.goto("/approve/original-purchase");
  await expect(
    page.getByText("https://seller2.example", { exact: true }),
  ).toBeVisible();
  expect(context).toBe("original-purchase");
});
