import { readFileSync } from "node:fs";
import { expect, test, type Page, type Route } from "@playwright/test";

const original = {
  ...JSON.parse(
    readFileSync(
      new URL("../../examples/diff-review/service.json", import.meta.url),
      "utf-8",
    ),
  ),
  revision: "1".repeat(64),
  network: "eip155:84532",
  asset: "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
  protocol: "hermes-sell/0.1",
};
const second = {
  ...JSON.parse(
    readFileSync(
      new URL("../../examples/release-summary/service.json", import.meta.url),
      "utf-8",
    ),
  ),
  revision: "2".repeat(64),
  network: original.network,
  asset: original.asset,
  protocol: original.protocol,
};

function respond(route: Route, body: unknown, status = 200) {
  return route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

function deferred() {
  let resolve!: () => void;
  const promise = new Promise<void>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

async function mockPublic(
  page: Page,
  options: {
    expireFirstSession?: boolean;
    quoteGate?: ReturnType<typeof deferred>;
    rejectRevisionMismatch?: boolean;
  } = {},
) {
  const state = {
    services: structuredClone([original, second]),
    sessions: 0,
    admissions: 0,
    quotes: 0,
    order: undefined as any,
  };
  await page.route("**/dev/session", (route) => {
    state.sessions += 1;
    return respond(route, {
      token: `review-session-${state.sessions}`,
      owner: "demo-browser",
      expires_at: Math.floor(Date.now() / 1000) + 900,
    });
  });
  await page.route("**/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    const method = route.request().method();
    if (path === "/v1/services")
      return respond(route, { services: state.services, mode: "simulation" });
    if (path.startsWith("/v1/services/") && method === "GET") {
      const slug = path.split("/")[3];
      const service = path.includes(original.revision)
        ? original
        : state.services.find((item) => item.slug === slug);
      return respond(route, service);
    }
    if (path.startsWith("/v1/services/") && method === "POST") {
      state.quotes += 1;
      const body = route.request().postDataJSON();
      const service = structuredClone(
        state.services.find((item) => item.slug === path.split("/")[3]),
      );
      if (options.rejectRevisionMismatch && body.revision !== service.revision)
        return respond(
          route,
          {
            error: {
              code: "revision_changed",
              message:
                "The service version has changed. Review the price and details again.",
            },
          },
          409,
        );
      state.order = {
        id: "review-order-fixed",
        slug: service.slug,
        revision: body.revision,
        input: body.input,
        origin: "http://127.0.0.1:8875",
        amount: service.amount,
        pay_to: service.pay_to,
        network: service.network,
        asset: service.asset,
        quote_expires_at: Math.floor(Date.now() / 1000) + 300,
        order_digest: "a".repeat(64),
        execution: "created",
        payment: "required",
        mode: "simulation",
        service_contract: service,
      };
      await options.quoteGate?.promise;
      return respond(route, state.order, 201);
    }
    if (path.startsWith("/v1/orders/") && method === "POST") {
      state.admissions += 1;
      state.order = {
        ...state.order,
        execution: "running",
        payment: "verified",
      };
      return respond(route, state.order, 202);
    }
    if (path.startsWith("/v1/orders/") && method === "GET") {
      if (
        options.expireFirstSession &&
        state.sessions === 1 &&
        state.admissions > 0
      ) {
        return respond(
          route,
          {
            error: {
              code: "authentication_required",
              message: "Reconnect your session.",
            },
          },
          401,
        );
      }
      if (state.admissions > 0)
        state.order = {
          ...state.order,
          execution: "result_ready",
          payment: "settled",
          result: original.examples[0].output,
          receipt: {
            success: true,
            transaction: "simulation:review-order-fixed",
          },
        };
      return respond(route, state.order);
    }
    return route.fallback();
  });
  return state;
}

async function connectAndQuote(page: Page) {
  await page.goto("/s/diff-review");
  await page
    .getByRole("button", { name: "Connect test session", exact: true })
    .click();
  await page.getByRole("button", { name: "Review quote", exact: true }).click();
}

test("pending quote prevents switching the displayed service", async ({
  page,
}) => {
  const gate = deferred();
  const state = await mockPublic(page, { quoteGate: gate });
  try {
    await connectAndQuote(page);
    await expect.poll(() => state.quotes).toBe(1);
    await expect(
      page.getByRole("button", { name: "Release summary", exact: true }),
    ).toBeDisabled();
  } finally {
    gate.resolve();
  }
  await expect(
    page.getByRole("button", { name: "Approve 0.10 USDC simulation" }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Diff review", exact: true }),
  ).toBeVisible();
});

test("expired session reconnects and recovers the original order without authorizing again", async ({
  page,
}) => {
  const state = await mockPublic(page, { expireFirstSession: true });
  await connectAndQuote(page);
  await page
    .getByRole("button", { name: "Approve 0.10 USDC simulation" })
    .click();
  const reconnect = page.getByRole("button", {
    name: /Reconnect and restore order/,
  });
  await expect(reconnect).toBeVisible();
  await reconnect.click();
  await expect(
    page.getByRole("heading", { name: "Result", exact: true }),
  ).toBeVisible();
  expect(state.admissions).toBe(1);
  expect(state.quotes).toBe(1);
  expect(state.sessions).toBe(2);
});

test("restored order displays its frozen contract after the service is republished", async ({
  page,
}) => {
  const state = await mockPublic(page);
  await connectAndQuote(page);
  await page
    .getByRole("button", { name: "Approve 0.10 USDC simulation" })
    .click();
  await expect(
    page.getByRole("heading", { name: "Result", exact: true }),
  ).toBeVisible();
  state.services[0] = {
    ...state.services[0],
    name: "Updated review service",
    revision: "3".repeat(64),
    amount: "9000000",
    input_schema: {
      type: "object",
      additionalProperties: false,
      required: ["replacement"],
      properties: {
        replacement: { type: "string", title: "New input", maxLength: 100 },
      },
    },
    examples: [
      {
        input: { replacement: "New version example" },
        output: original.examples[0].output,
      },
    ],
  };
  await page.reload();
  await page
    .getByRole("button", { name: "Reconnect and restore order", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "Diff review", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Updated review service", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByLabel("Diff to review *", { exact: true }),
  ).toHaveValue(original.examples[0].input.diff);
  expect(state.admissions).toBe(1);
});

test("pending draft save prevents template and new-service switches", async ({
  page,
}) => {
  const gate = deferred();
  let saves = 0;
  await page.route("**/admin/drafts", async (route) => {
    saves += 1;
    const draft = route.request().postDataJSON();
    await gate.promise;
    return respond(route, { ...draft.spec, revision: "4".repeat(64) }, 201);
  });
  await page.goto("http://127.0.0.1:8876/");
  await page
    .getByLabel("Admin token", { exact: true })
    .fill("local-preview-only");
  await page.getByRole("button", { name: "Open workspace" }).click();
  await page.getByRole("button", { name: "New service", exact: true }).click();
  try {
    await page
      .getByRole("button", { name: "Save version", exact: true })
      .click();
    await expect.poll(() => saves).toBe(1);
    await expect(
      page.getByRole("button", { name: "New service", exact: true }),
    ).toBeDisabled();
    await expect(
      page.getByLabel("Select example skill", { exact: true }),
    ).toBeDisabled();
  } finally {
    gate.resolve();
  }
  await expect(
    page.getByRole("button", { name: "Run simulation test", exact: true }),
  ).toBeEnabled();
});

test("micro-price remains exact in quote and approval label", async ({
  page,
}) => {
  const state = await mockPublic(page);
  state.services[0].amount = "1";
  await connectAndQuote(page);
  await expect(
    page.getByRole("button", {
      name: "Approve 0.000001 USDC simulation",
      exact: true,
    }),
  ).toBeVisible();
  expect(state.order.amount).toBe("1");
});

test("revision conflict requires explicit contract refresh and a new quote click", async ({
  page,
}) => {
  const state = await mockPublic(page, { rejectRevisionMismatch: true });
  await page.goto("/s/diff-review");
  await page
    .getByRole("button", { name: "Connect test session", exact: true })
    .click();
  await page
    .getByLabel("Diff to review *", { exact: true })
    .fill("+keep my edit");
  state.services[0] = {
    ...state.services[0],
    name: "Review service with a new contract",
    revision: "6".repeat(64),
    amount: "250000",
    input_schema: {
      type: "object",
      additionalProperties: false,
      required: ["diff"],
      properties: { diff: original.input_schema.properties.diff },
    },
    examples: [
      { input: { diff: "+new example" }, output: original.examples[0].output },
    ],
  };
  await page.getByRole("button", { name: "Review quote", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Review latest contract", exact: true }),
  ).toBeVisible();
  expect(state.quotes).toBe(1);
  expect(state.admissions).toBe(0);
  await page
    .getByRole("button", { name: "Review latest contract", exact: true })
    .click();
  await expect(
    page.getByRole("heading", {
      name: "Review service with a new contract",
      exact: true,
    }),
  ).toBeVisible();
  await expect(page.locator(".price")).toHaveText("0.25 USDC");
  await expect(
    page.getByLabel("Diff to review *", { exact: true }),
  ).toHaveValue("+keep my edit");
  await expect(
    page.getByLabel("Review criteria *", { exact: true }),
  ).toHaveCount(0);
  expect(state.quotes).toBe(1);
  expect(state.admissions).toBe(0);
  await page.getByRole("button", { name: "Review quote", exact: true }).click();
  await expect(
    page.getByRole("button", {
      name: "Approve 0.25 USDC simulation",
      exact: true,
    }),
  ).toBeVisible();
  expect(state.quotes).toBe(2);
  expect(state.order.revision).toBe("6".repeat(64));
  expect(state.order.input).toEqual({ diff: "+keep my edit" });
  expect(state.admissions).toBe(0);
});

test("example input clears a previous array validation error", async ({
  page,
}) => {
  const state = await mockPublic(page);
  state.services[0].input_schema = {
    type: "object",
    additionalProperties: false,
    required: ["tags"],
    properties: {
      tags: {
        type: "array",
        title: "Tags",
        maxItems: 4,
        items: { type: "string", maxLength: 50 },
      },
    },
  };
  state.services[0].examples = [
    { input: { tags: ["initial"] }, output: original.examples[0].output },
  ];
  await page.goto("/s/diff-review");
  const arrayInput = page.getByLabel("Tags *", { exact: true });
  await arrayInput.fill('["invalid"');
  await expect
    .poll(() =>
      arrayInput.evaluate((element: HTMLTextAreaElement) =>
        element.checkValidity(),
      ),
    )
    .toBe(false);
  await page.getByRole("button", { name: "Use example", exact: true }).click();
  await expect
    .poll(() =>
      arrayInput.evaluate((element: HTMLTextAreaElement) =>
        element.checkValidity(),
      ),
    )
    .toBe(true);
  await page
    .getByRole("button", { name: "Connect test session", exact: true })
    .click();
  await page.getByRole("button", { name: "Review quote", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Approve 0.10 USDC simulation" }),
  ).toBeVisible();
  expect(state.order.input).toEqual({ tags: ["initial"] });
});

test("titleless labels preserve paths and omitted optional fields stay optional", async ({
  page,
}) => {
  const state = await mockPublic(page);
  state.services[0].input_schema = {
    type: "object",
    additionalProperties: false,
    required: ["diff"],
    properties: {
      diff: {
        type: "string",
        minLength: 1,
        maxLength: 1000,
      },
      options: {
        type: "object",
        additionalProperties: false,
        required: ["review_language"],
        properties: {
          review_language: {
            type: "string",
            minLength: 1,
            maxLength: 30,
          },
        },
      },
    },
  };
  state.services[0].examples = [
    { input: { diff: "+safe_change" }, output: original.examples[0].output },
  ];
  await page.goto("/s/diff-review");
  await expect(page.getByLabel("Diff *", { exact: true })).toHaveAttribute(
    "id",
    "input.diff",
  );
  await page.getByLabel("Include Options", { exact: true }).check();
  await expect(
    page.getByLabel("Review language *", { exact: true }),
  ).toHaveAttribute("id", "input.options.review_language");
  await page.getByLabel("Include Options", { exact: true }).uncheck();
  await page
    .getByRole("button", { name: "Connect test session", exact: true })
    .click();
  await page.getByRole("button", { name: "Review quote", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Approve 0.10 USDC simulation" }),
  ).toBeVisible();
  expect(state.order.input).toEqual({ diff: "+safe_change" });
});

test("boolean enum preserves false as a boolean in the quoted input", async ({
  page,
}) => {
  const state = await mockPublic(page);
  state.services[0].input_schema = {
    type: "object",
    additionalProperties: false,
    required: ["enabled"],
    properties: {
      enabled: { type: "boolean", title: "Enabled", enum: [true, false] },
    },
  };
  state.services[0].examples = [
    { input: { enabled: true }, output: original.examples[0].output },
  ];
  await page.goto("/s/diff-review");
  await page
    .getByLabel("Enabled *", { exact: true })
    .selectOption({ label: "false" });
  await page
    .getByRole("button", { name: "Connect test session", exact: true })
    .click();
  await page.getByRole("button", { name: "Review quote", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Approve 0.10 USDC simulation" }),
  ).toBeVisible();
  expect(state.order.input.enabled).toBe(false);
});

test("advanced schema JSON cannot override visible price or payee", async ({
  page,
}) => {
  let saves = 0;
  await page.route("**/admin/drafts", async (route) => {
    saves += 1;
    return respond(
      route,
      { ...route.request().postDataJSON().spec, revision: "5".repeat(64) },
      201,
    );
  });
  await page.goto("http://127.0.0.1:8876/");
  await page
    .getByLabel("Admin token", { exact: true })
    .fill("local-preview-only");
  await page.getByRole("button", { name: "Open workspace" }).click();
  await page.getByRole("button", { name: "New service", exact: true }).click();
  await page
    .getByText("Edit input/output schemas and examples", { exact: true })
    .click();
  const editor = page.getByLabel("Input/output contract JSON", { exact: true });
  const contract = JSON.parse(await editor.inputValue());
  await editor.fill(
    JSON.stringify(
      {
        ...contract,
        amount: "9000000",
        pay_to: "0x2222222222222222222222222222222222222222",
      },
      null,
      2,
    ),
  );
  await page.getByRole("button", { name: "Save version", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText(
    "price and payee address",
  );
  expect(saves).toBe(0);
});

for (const { theme, width } of [
  { theme: "light" as const, width: 1280 },
  { theme: "dark" as const, width: 1280 },
  { theme: "dark" as const, width: 360 },
])
  test(
    "Studio builder fits " + width + "px in " + theme + " theme",
    async ({ page }) => {
      await page.setViewportSize({ width, height: 1000 });
      await page.emulateMedia({ colorScheme: theme });
      await page.goto("http://127.0.0.1:8876/");
      await page
        .getByLabel("Admin token", { exact: true })
        .fill("local-preview-only");
      await page.getByRole("button", { name: "Open workspace" }).click();
      await page
        .getByRole("button", { name: "New service", exact: true })
        .click();
      await expect(
        page.getByLabel("Service name", { exact: true }),
      ).toBeVisible();
      await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
      expect(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
      ).toBe(true);
      await page.screenshot({
        path: "test-results/studio-builder-" + theme + "-" + width + ".png",
        fullPage: true,
      });
    },
  );
