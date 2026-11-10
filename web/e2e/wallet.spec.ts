import { expect, test, type Page, type Route } from "@playwright/test";
import { readFileSync } from "node:fs";
import { createHash } from "node:crypto";
import { formatSIWEMessage } from "@x402/extensions/sign-in-with-x";
import { privateKeyToAccount } from "viem/accounts";
import { recoverMessageAddress, recoverTypedDataAddress, type Hex } from "viem";

const fixture = JSON.parse(
  readFileSync(new URL("./fixtures/payment-v2.json", import.meta.url), "utf8"),
);
// This public, unfunded fixture account signs in Node only. No key enters the page.
const signer = privateKeyToAccount(("0x" + "11".repeat(32)) as Hex);
const origin = fixture.order.origin;
const reply = (route: Route, body: unknown, status = 200) =>
  route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });

async function injectedWallet(page: Page) {
  const state = {
    chain: "0x14a34",
    address: signer.address as string,
    loginSignatures: 0,
    paymentSignatures: 0,
    rejectNextPayment: false,
    paymentGate: undefined as Promise<void> | undefined,
    typedData: [] as any[],
  };
  await page.exposeFunction(
    "fixtureWalletRequest",
    async (args: { method: string; params?: any[] }) => {
      switch (args.method) {
        case "eth_requestAccounts":
        case "eth_accounts":
          return [state.address];
        case "eth_chainId":
          return state.chain;
        case "eth_getCode":
          return "0x";
        case "personal_sign": {
          state.loginSignatures++;
          return signer.signMessage({
            message: { raw: args.params![0] as Hex },
          });
        }
        case "eth_signTypedData_v4": {
          state.paymentSignatures++;
          await state.paymentGate;
          if (state.rejectNextPayment) {
            state.rejectNextPayment = false;
            return {
              fixtureError: { code: 4001, message: "User rejected request" },
            };
          }
          const typed = JSON.parse(args.params![1]);
          state.typedData.push(typed);
          return signer.signTypedData(typed);
        }
        default:
          throw new Error("Unexpected wallet method: " + args.method);
      }
    },
  );
  await page.addInitScript(() => {
    const listeners = new Map<string, Set<(...args: unknown[]) => void>>();
    const target = window as any;
    target.ethereum = {
      request: async (args: unknown) => {
        const result = await target.fixtureWalletRequest(args);
        if (result?.fixtureError)
          throw Object.assign(new Error(result.fixtureError.message), {
            code: result.fixtureError.code,
          });
        return result;
      },
      on: (event: string, listener: (...args: unknown[]) => void) => {
        if (!listeners.has(event)) listeners.set(event, new Set());
        listeners.get(event)!.add(listener);
      },
      removeListener: (event: string, listener: (...args: unknown[]) => void) =>
        listeners.get(event)?.delete(listener),
    };
    target.fixtureWalletEvent = (event: string, value: unknown) => {
      for (const listener of listeners.get(event) || []) listener(value);
    };
  });
  await page.clock.setFixedTime(new Date(fixture.now * 1000));
  return state;
}

function challenge(seller = origin) {
  const payload = {
    domain: new URL(seller).host,
    address: signer.address,
    uri: seller + "/auth/verify",
    version: "1",
    chainId: "eip155:84532",
    type: "eip191" as const,
    nonce: "b".repeat(64),
    issuedAt: new Date(fixture.now * 1000).toISOString().replace(".000Z", "Z"),
    expirationTime: new Date((fixture.now + 300) * 1000)
      .toISOString()
      .replace(".000Z", "Z"),
    statement:
      "Sign in to Hermes Sell to view your orders. This does not authorize a payment.",
    signatureScheme: "eip191" as const,
  };
  return {
    nonce: payload.nonce,
    payload,
    message: formatSIWEMessage(payload, payload.address),
    expires_at: fixture.now + 300,
  };
}

async function authRoutes(
  page: Page,
  options: { buyer?: boolean; seller?: string; wrongDomain?: boolean } = {},
) {
  const prefix = options.buyer ? "/buyer/auth" : "/auth";
  const auth = challenge(
    options.wrongDomain ? "https://different.example" : options.seller,
  );
  await page.route("**" + prefix + "/challenge", async (route) => {
    expect(route.request().postDataJSON().address.toLowerCase()).toBe(
      signer.address.toLowerCase(),
    );
    return reply(route, auth);
  });
  await page.route("**" + prefix + "/verify", async (route) => {
    const { payload } = route.request().postDataJSON();
    const { signature, ...unsigned } = payload;
    expect(unsigned).toEqual(auth.payload);
    expect(
      (
        await recoverMessageAddress({ message: auth.message, signature })
      ).toLowerCase(),
    ).toBe(signer.address.toLowerCase());
    return reply(route, {
      token: options.buyer ? undefined : "fixture-wallet-session",
      connected: options.buyer ? true : undefined,
      owner: signer.address.toLowerCase(),
      address: signer.address,
      network: "eip155:84532",
      expires_at: fixture.now + 900,
    });
  });
}

async function storefront(
  page: Page,
  options: {
    loseFirst?: boolean;
    wrongDomain?: boolean;
    wrongNonce?: boolean;
  } = {},
) {
  const wallet = await injectedWallet(page);
  const order = structuredClone(fixture.order);
  if (options.wrongNonce) order.payment_nonce = "0x" + "c".repeat(64);
  const admissions: unknown[] = [];
  await page.route("**/ui-config", (route) =>
    reply(route, {
      role: "storefront",
      mode: "testnet",
      origin,
      auth: "siwx",
      network: order.network,
      asset: order.asset,
      execution_mode: "hermes",
    }),
  );
  await authRoutes(page, options);
  await page.route("**/v1/services", (route) =>
    reply(route, { services: [order.service_contract], mode: "testnet" }),
  );
  await page.route("**/v1/services/diff-review/orders", (route) =>
    reply(route, order, 201),
  );
  await page.route("**/v1/orders/" + order.id, async (route) => {
    if (route.request().method() === "POST") {
      const body = route.request().postDataJSON();
      admissions.push(body.authorization);
      expect(body.authorization).toEqual(fixture.payload);
      if (options.loseFirst && admissions.length === 1)
        return route.abort("failed");
      Object.assign(order, {
        execution: "result_ready",
        payment: "settled",
        result: order.service_contract.examples[0].output,
        receipt: {
          fixture: true,
          network: order.network,
          transaction: "0x" + "d".repeat(64),
        },
      });
    }
    return reply(route, order);
  });
  return { wallet, order, admissions };
}

async function quote(page: Page) {
  await page.goto("/s/diff-review");
  await page
    .getByRole("button", { name: "Connect wallet", exact: true })
    .click();
  await page.getByRole("button", { name: "Review quote", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "Approve 0.10 USDC on testnet" }),
  ).toBeVisible();
}

test("injected EOA signs real SIWX and exact order-bound EIP-712, with no chain transaction", async ({
  page,
}) => {
  const { wallet, admissions } = await storefront(page);
  await page.setViewportSize({ width: 360, height: 900 });
  await quote(page);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({
    path: "test-results/testnet-storefront-360.png",
    fullPage: true,
  });
  expect(wallet.loginSignatures).toBe(1);
  expect(wallet.paymentSignatures).toBe(0);
  expect(admissions).toHaveLength(0);
  await expect(page.getByText(origin, { exact: true })).toBeVisible();
  await page
    .getByRole("button", { name: "Approve 0.10 USDC on testnet" })
    .click();
  await expect(
    page.getByRole("heading", { name: "Result", exact: true }),
  ).toBeVisible();
  expect(wallet.paymentSignatures).toBe(1);
  expect(admissions).toHaveLength(1);
  const recovered = await recoverTypedDataAddress({
    ...wallet.typedData[0],
    signature: fixture.payload.payload.signature,
  });
  expect(recovered.toLowerCase()).toBe(signer.address.toLowerCase());
  await page.reload();
  await page
    .getByRole("button", { name: "Reconnect and restore order" })
    .click();
  await expect(
    page.getByRole("heading", { name: "Result", exact: true }),
  ).toBeVisible();
  expect(wallet.paymentSignatures).toBe(1);
  expect(admissions).toHaveLength(1);
  const stored = await page.evaluate(() =>
    JSON.stringify({
      local: { ...localStorage },
      session: { ...sessionStorage },
    }),
  );
  expect(stored).not.toContain(fixture.payload.payload.signature);
  expect(stored).not.toContain("fixture-wallet-session");
});

test("a lost admission response reuses the identical signature and nonce", async ({
  page,
}) => {
  const { wallet, admissions } = await storefront(page, { loseFirst: true });
  await quote(page);
  await page
    .getByRole("button", { name: "Approve 0.10 USDC on testnet" })
    .click();
  await expect(
    page.getByRole("button", { name: "Retry same approval" }),
  ).toBeEnabled();
  await page.getByRole("button", { name: "Retry same approval" }).click();
  await expect(
    page.getByRole("heading", { name: "Result", exact: true }),
  ).toBeVisible();
  expect(admissions).toHaveLength(2);
  expect(admissions[0]).toEqual(admissions[1]);
  expect(wallet.paymentSignatures).toBe(1);
});

test("a review uses a separate personal signature and retries it without another payment", async ({
  page,
}) => {
  const { wallet, order } = await storefront(page);
  const directory = "http://127.0.0.1:8879";
  await page.route("**/ui-config", (route) =>
    reply(route, {
      role: "storefront",
      mode: "testnet",
      origin,
      directory_origin: directory,
      auth: "siwx",
      network: order.network,
      asset: order.asset,
      features: ["reviews"],
    }),
  );
  let mismatched = true;
  let challenges = 0;
  const signatures: string[] = [];
  await page.route("**/review-challenge", (route) => {
    challenges++;
    const { score, comment } = route.request().postDataJSON();
    const payload = {
      directory_origin: mismatched ? "https://foreign.example" : directory,
      listing_id: createHash("sha256")
        .update(JSON.stringify(["hermes-sell-listing-v1", origin, order.slug]))
        .digest("hex"),
      order_id: order.id,
      payer: order.owner,
      score,
      comment,
      nonce: "f".repeat(64),
      expires_at: fixture.now + 300,
    };
    const message = `Publish a Hermes Sell review\nDirectory: ${payload.directory_origin}\nListing: ${payload.listing_id}\nOrder: ${payload.order_id}\nRating: ${score}/5\nComment: ${JSON.stringify(comment)}\nWallet: ${payload.payer}\nNonce: ${payload.nonce}\nExpires: ${payload.expires_at}\nThis signature publishes a review only. It does not authorize a payment.`;
    return reply(route, {
      payload,
      message,
      challenge_id: payload.nonce,
      mode: "testnet",
    });
  });
  await page.route("**/review", async (route) => {
    signatures.push(route.request().postDataJSON().signature);
    if (signatures.length === 1) return route.abort("failed");
    return reply(route, { summary: { count: 1, average: 4 }, reviews: [] });
  });
  await quote(page);
  await page
    .getByRole("button", { name: "Approve 0.10 USDC on testnet", exact: true })
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
    .fill("Useful review finding.");
  await page
    .getByRole("button", { name: "Sign and publish review", exact: true })
    .click();
  await expect(
    page.getByText(
      "The review signature request does not match this purchase, rating or directory.",
      { exact: true },
    ),
  ).toBeVisible();
  expect(wallet.loginSignatures).toBe(1);
  mismatched = false;
  await page
    .getByRole("button", { name: "Sign and publish review", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "Retry same review", exact: true }),
  ).toBeEnabled();
  expect(wallet.loginSignatures).toBe(2);
  await page
    .getByRole("button", { name: "Retry same review", exact: true })
    .click();
  await expect(page.getByText(/Your review is published/)).toBeVisible();
  expect(signatures).toHaveLength(2);
  expect(signatures[0]).toBe(signatures[1]);
  expect(challenges).toBe(2);
  expect(wallet.loginSignatures).toBe(2);
  expect(wallet.paymentSignatures).toBe(1);
});

test("an unsigned quote survives reload and can receive its first approval", async ({
  page,
}) => {
  const { wallet, admissions } = await storefront(page);
  await quote(page);
  await page.reload();
  await page
    .getByRole("button", { name: "Reconnect and restore order" })
    .click();
  await page
    .getByRole("button", { name: "Approve 0.10 USDC on testnet" })
    .click();
  await expect(
    page.getByRole("heading", { name: "Result", exact: true }),
  ).toBeVisible();
  expect(wallet.paymentSignatures).toBe(1);
  expect(admissions).toHaveLength(1);
});

test("two tabs cannot generate separate signatures for one restored quote", async ({
  page,
  context,
}) => {
  const first = await storefront(page);
  await quote(page);
  const other = await context.newPage();
  const second = await storefront(other);
  let release!: () => void;
  first.wallet.paymentGate = new Promise<void>((resolve) => {
    release = resolve;
  });
  try {
    await other.goto("/s/diff-review");
    await other
      .getByRole("button", { name: "Reconnect and restore order" })
      .click();
    await expect(
      other.getByRole("button", { name: "Approve 0.10 USDC on testnet" }),
    ).toBeVisible();
    await page
      .getByRole("button", { name: "Approve 0.10 USDC on testnet" })
      .click();
    await expect.poll(() => first.wallet.paymentSignatures).toBe(1);
    await other
      .getByRole("button", { name: "Approve 0.10 USDC on testnet" })
      .click();
    await expect(other.getByRole("alert")).toContainText(
      "approved in another tab",
    );
    expect(second.wallet.paymentSignatures).toBe(0);
    release();
    await expect(
      page.getByRole("heading", { name: "Result", exact: true }),
    ).toBeVisible();
    await other.reload();
    await other
      .getByRole("button", { name: "Reconnect and restore order" })
      .click();
    await expect(
      other.getByRole("button", { name: "Refresh original order" }),
    ).toBeVisible();
    await expect(
      other.getByRole("button", { name: "Approve 0.10 USDC on testnet" }),
    ).toHaveCount(0);
    expect(first.admissions).toHaveLength(1);
    expect(second.admissions).toHaveLength(0);
  } finally {
    release();
    await other.close();
  }
});

test("legacy restored quotes with no intent metadata remain protected from re-signing", async ({
  page,
}) => {
  const { wallet, order } = await storefront(page);
  await page.addInitScript((id) => {
    localStorage.setItem("hermes-sell:last-order", id);
  }, order.id);
  await page.goto("/s/diff-review");
  await page
    .getByRole("button", { name: "Reconnect and restore order" })
    .click();
  await expect(
    page.getByRole("button", { name: "Refresh original order" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Approve 0.10 USDC on testnet" }),
  ).toHaveCount(0);
  expect(wallet.paymentSignatures).toBe(0);
});

test("reload after an uncertain admission never generates a second payment signature", async ({
  page,
}) => {
  const { wallet, admissions } = await storefront(page, { loseFirst: true });
  await quote(page);
  await page
    .getByRole("button", { name: "Approve 0.10 USDC on testnet" })
    .click();
  await expect(
    page.getByRole("button", { name: "Retry same approval" }),
  ).toBeEnabled();
  await page.reload();
  await page
    .getByRole("button", { name: "Reconnect and restore order" })
    .click();
  await expect(
    page.getByRole("button", { name: "Refresh original order" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Approve 0.10 USDC on testnet" }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "Retry same approval" }),
  ).toHaveCount(0);
  await page.clock.setFixedTime(new Date((fixture.now + 301) * 1000));
  await page.getByRole("button", { name: "Refresh original order" }).click();
  await expect(
    page.getByRole("button", { name: "Start new order" }),
  ).toBeDisabled();
  await page.clock.setFixedTime(new Date((fixture.now + 481) * 1000));
  await expect(
    page.getByRole("button", { name: "Start new order" }),
  ).toBeDisabled();
  await page.getByRole("button", { name: "Refresh original order" }).click();
  await expect(
    page.getByRole("button", { name: "Start new order" }),
  ).toBeEnabled();
  expect(wallet.paymentSignatures).toBe(1);
  expect(admissions).toHaveLength(1);
});

test("wallet rejection leaves the quote unsigned and allows a new explicit approval", async ({
  page,
}) => {
  const { wallet, admissions } = await storefront(page);
  wallet.rejectNextPayment = true;
  await quote(page);
  await page
    .getByRole("button", { name: "Approve 0.10 USDC on testnet" })
    .click();
  await expect(page.getByRole("alert")).toContainText(
    "Payment approval was rejected",
  );
  expect(admissions).toHaveLength(0);
  await page
    .getByRole("button", { name: "Approve 0.10 USDC on testnet" })
    .click();
  await expect(
    page.getByRole("heading", { name: "Result", exact: true }),
  ).toBeVisible();
  expect(wallet.typedData).toHaveLength(1);
});

test("a failed saved-order restore cannot unlock a replacement quote", async ({
  page,
}) => {
  const { wallet, order } = await storefront(page);
  await quote(page);
  await page.reload();
  await page.route("**/v1/orders/" + order.id, (route) =>
    reply(
      route,
      { error: { code: "not_found", message: "Order not found." } },
      404,
    ),
  );
  await page
    .getByRole("button", { name: "Reconnect and restore order" })
    .click();
  await expect(page.getByRole("alert")).toContainText(
    "saved order is not available",
  );
  await expect(
    page.getByRole("button", { name: "Reconnect and restore order" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Review quote", exact: true }),
  ).toHaveCount(0);
  expect(wallet.paymentSignatures).toBe(0);
});

test("wrong-chain wallets and foreign SIWX domains are blocked before signing", async ({
  page,
}) => {
  const { wallet } = await storefront(page, { wrongDomain: true });
  wallet.chain = "0x1";
  await page.goto("/s/diff-review");
  await page
    .getByRole("button", { name: "Connect wallet", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText("Base Sepolia");
  expect(wallet.loginSignatures).toBe(0);
  wallet.chain = "0x14a34";
  await page
    .getByRole("button", { name: "Connect wallet", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText(
    "sign-in challenge does not match",
  );
  expect(wallet.loginSignatures).toBe(0);
  expect(wallet.paymentSignatures).toBe(0);
});

test("a mismatched payment nonce cannot reach the wallet signer", async ({
  page,
}) => {
  const { wallet, admissions } = await storefront(page, { wrongNonce: true });
  await quote(page);
  await page
    .getByRole("button", { name: "Approve 0.10 USDC on testnet" })
    .click();
  await expect(page.getByRole("alert")).toContainText(
    "payment requirements do not match",
  );
  expect(wallet.paymentSignatures).toBe(0);
  expect(admissions).toHaveLength(0);
});

test("wallet account changes invalidate the browser session without signing", async ({
  page,
}) => {
  const { wallet, admissions } = await storefront(page);
  await quote(page);
  wallet.address = "0x2222222222222222222222222222222222222222";
  await page.evaluate(() =>
    (window as any).fixtureWalletEvent("accountsChanged", [
      "0x2222222222222222222222222222222222222222",
    ]),
  );
  await expect(page.getByRole("alert")).toContainText(
    "wallet account or network changed",
  );
  await expect(
    page.getByRole("button", { name: "Approve 0.10 USDC on testnet" }),
  ).toHaveCount(0);
  expect(wallet.paymentSignatures).toBe(0);
  expect(admissions).toHaveLength(0);
});

test("local buyer retries the same immutable approval without submitting the purchase", async ({
  page,
}) => {
  const wallet = await injectedWallet(page);
  const seller = "https://seller.example";
  const order = { ...structuredClone(fixture.order), origin: seller };
  const purchase = {
    purchase_id: "buyer-fixture",
    status: "awaiting_approval",
    order,
  };
  let connected = false;
  const approvals: any[] = [];
  await page.route("**/ui-config", (route) =>
    reply(route, {
      role: "buyer",
      mode: "testnet",
      origin,
      seller_origin: seller,
      auth: "siwx",
      network: order.network,
      asset: order.asset,
    }),
  );
  await authRoutes(page, { buyer: true, seller });
  await page.route("**/buyer/session", (route) =>
    reply(route, {
      connected,
      owner: signer.address.toLowerCase(),
      address: signer.address,
      expires_at: fixture.now + 900,
    }),
  );
  page.on("response", (response) => {
    if (response.url().endsWith("/buyer/auth/verify")) connected = true;
  });
  await page.route("**/buyer/purchases/buyer-fixture", (route) =>
    reply(route, purchase),
  );
  await page.route(
    "**/buyer/purchases/buyer-fixture/approve",
    async (route) => {
      approvals.push(route.request().postDataJSON());
      expect(approvals[0].order_digest).toBe(order.order_digest);
      expect(approvals[0].authorization.payload.authorization.nonce).toBe(
        order.payment_nonce,
      );
      expect(approvals[0].authorization.resource.url).toBe(
        seller + "/v1/orders/" + order.id,
      );
      if (approvals.length === 1) return route.abort("failed");
      expect(approvals[1]).toEqual(approvals[0]);
      purchase.status = "approved";
      return reply(route, purchase);
    },
  );
  await page.route("**/approve/buyer-fixture", async (route) => {
    const response = await page.request.get("/");
    return route.fulfill({ response });
  });
  await page.goto("/approve/buyer-fixture");
  await page.setViewportSize({ width: 360, height: 900 });
  await expect(page.getByText(seller, { exact: true })).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: "test-results/buyer-approval-360.png",
    fullPage: true,
  });
  await page
    .getByRole("button", { name: "Connect wallet", exact: true })
    .click();
  await expect(
    page.getByText("Wallet connected", { exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Approve 0.10 USDC for agent" })
    .click();
  await expect(
    page.getByRole("button", { name: "Retry same approval" }),
  ).toBeEnabled();
  await page.getByRole("button", { name: "Retry same approval" }).click();
  await expect(
    page.getByText("Approved. Ask your agent to submit this purchase."),
  ).toBeVisible();
  expect(wallet.paymentSignatures).toBe(1);
  expect(approvals).toHaveLength(2);
  await page.reload();
  await expect(page.getByText("Approved for agent submission")).toBeVisible();
  purchase.status = "unknown";
  await page.getByRole("button", { name: "Refresh purchase" }).click();
  await expect(
    page.getByText("Outcome unknown · Check original purchase"),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Approve 0.10 USDC for agent" }),
  ).toHaveCount(0);
  expect(wallet.paymentSignatures).toBe(1);
});

test("Studio imports a Hermes bundle and tests it without dropping reference files", async ({
  page,
}) => {
  const service = fixture.order.service_contract;
  let published = false;
  let imported: unknown;
  await page.route("**/ui-config", (route) =>
    reply(route, { role: "studio", mode: "testnet", execution_mode: "hermes" }),
  );
  await page.route("**/admin/revisions", (route) =>
    reply(route, { revisions: [{ public: service, published }] }),
  );
  await page.route("**/admin/revisions/" + service.revision, (route) =>
    reply(route, {
      public: service,
      bundle: {
        files: {
          "SKILL.md": "# Original skill",
          "references/rules.txt": "Preserve this reference.",
        },
      },
    }),
  );
  await page.route("**/admin/drafts/import", (route) => {
    imported = route.request().postDataJSON();
    return reply(route, service, 201);
  });
  await page.route(
    "**/admin/revisions/" + service.revision + "/test",
    (route) =>
      reply(route, {
        id: "execution-test",
        passed: true,
        mode: "hermes",
        cases: [{ output: { summary: "Executed fixture" }, passed: true }],
      }),
  );
  await page.route(
    "**/admin/revisions/" + service.revision + "/publish",
    (route) => {
      expect(route.request().postDataJSON()).toEqual({
        report_id: "execution-test",
        owner_reviewed: true,
      });
      published = true;
      return reply(route, service);
    },
  );
  await page.goto("http://127.0.0.1:8876/");
  await page
    .getByLabel("Admin token", { exact: true })
    .fill("local-preview-only");
  await page.getByRole("button", { name: "Open workspace" }).click();
  await page.getByText("Import Hermes draft", { exact: true }).click();
  await page
    .getByLabel("Hermes draft ID", { exact: true })
    .fill("draft-fixture");
  await page.getByLabel("Imported service URL slug").fill("diff-review");
  await page.getByLabel("Imported payee address").fill(service.pay_to);
  await page.getByRole("button", { name: "Import saved draft" }).click();
  await expect(
    page.getByRole("heading", { name: "Service definition" }),
  ).toBeVisible();
  expect(imported).toEqual({
    draft_id: "draft-fixture",
    slug: "diff-review",
    amount: "100000",
    pay_to: service.pay_to,
  });
  await expect(
    page.getByLabel("Original SKILL.md", { exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "Save version", exact: true }),
  ).toBeDisabled();
  await page.getByRole("button", { name: "Run execution test" }).click();
  await expect(
    page.getByText("Schema validation passed · Hermes execution output"),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "Publish service" }),
  ).toBeDisabled();
  await page
    .getByLabel(
      "I reviewed the execution result and approve publishing this version.",
    )
    .check();
  await page.getByRole("button", { name: "Publish service" }).click();
  await expect(
    page.getByText("Published to the testnet storefront."),
  ).toBeVisible();
});
