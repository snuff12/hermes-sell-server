import { expect, test } from "@playwright/test";

test("browser quotes, explicitly approves, receives result, and recovers without repaying", async ({
  page,
}) => {
  let admissions = 0;
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("request", (request) => {
    if (request.method() === "POST" && /\/v1\/orders\//.test(request.url()))
      admissions++;
  });
  await page.goto("/s/diff-review");
  await expect(
    page.getByRole("heading", { name: "Diff review" }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Connect test session", exact: true })
    .click();
  await page.getByRole("button", { name: "Review quote", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Approve 0.10 USDC simulation" }),
  ).toBeVisible();
  expect(admissions).toBe(0);
  await page
    .getByRole("button", { name: "Approve 0.10 USDC simulation" })
    .click();
  await expect(page.getByRole("heading", { name: "Result" })).toBeVisible();
  expect(admissions).toBe(1);
  await page.reload();
  await page
    .getByRole("button", { name: "Reconnect and restore order" })
    .click();
  await expect(page.getByRole("heading", { name: "Result" })).toBeVisible();
  expect(admissions).toBe(1);
  expect(errors).toEqual([]);
});

test("owner can edit, test, review and publish a new skill revision", async ({
  page,
}) => {
  await page.goto("http://127.0.0.1:8876/");
  await page
    .getByLabel("Admin token", { exact: true })
    .fill("local-preview-only");
  await page.getByRole("button", { name: "Open workspace" }).click();
  await page.getByRole("button", { name: "New service", exact: true }).click();
  await page
    .getByLabel("Service name", { exact: true })
    .fill("Review test service");
  await page
    .getByLabel("URL slug", { exact: true })
    .fill("browser-test-service");
  await page.getByRole("button", { name: "Save version", exact: true }).click();
  await page
    .getByRole("button", { name: "Run simulation test", exact: true })
    .click();
  await expect(
    page.getByText("Schema validation passed · Fixed example output"),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Publish locally", exact: true }),
  ).toBeDisabled();
  await page
    .getByLabel(
      "I reviewed the example result and understand this is a simulation.",
    )
    .check();
  await page
    .getByRole("button", { name: "Publish locally", exact: true })
    .click();
  await expect(
    page.getByText("Published to the local storefront.", { exact: false }),
  ).toBeVisible();
  await page.goto("/s/browser-test-service");
  await expect(
    page.getByRole("heading", { name: "Review test service", exact: true }),
  ).toBeVisible();
});

for (const width of [360, 1280])
  test(`storefront fits ${width}px and captures preview`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1000 });
    await page.goto("/s/diff-review");
    await expect(
      page.getByRole("heading", { name: "Diff review" }),
    ).toBeVisible();
    await expect(page.getByLabel("Diff to review *")).toBeVisible();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    await page.screenshot({
      path: `test-results/storefront-${width}.png`,
      fullPage: true,
    });
  });
