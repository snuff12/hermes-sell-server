// Real local testnet browser check. Secrets arrive on stdin and stay in Node.
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";

const require = createRequire(new URL("../web/package.json", import.meta.url));
const { chromium } = require("@playwright/test");
const { privateKeyToAccount } = require("viem/accounts");
const { createPublicClient, http } = require("viem");
const { baseSepolia } = require("viem/chains");
const input = JSON.parse(readFileSync(0, "utf8"));
if (!["connect", "approve", "purchase", "recover"].includes(input.action)) throw new Error("Choose an explicit test action");
const account = privateKeyToAccount(input.privateKey);
const rpc = createPublicClient({ chain: baseSepolia, transport: http("https://sepolia.base.org") });
const asset = "0x036CbD53842c5426634e7929541eC2318f3dCF7e";
const url = new URL(input.url);
if (url.protocol !== "http:" || url.hostname !== "127.0.0.1" || !["8785", "8767"].includes(url.port)) {
  throw new Error("This check supports the local testnet preview only.");
}
if ((await rpc.getChainId()) !== 84532) throw new Error("Wrong test chain");
const browser = await chromium.launch({ executablePath: process.env.CHROME_PATH || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" });
const page = await browser.newPage({ viewport: { width: 1280, height: 1000 } });
let loginSignatures = 0;
let paymentSignatures = 0;
let lastOrder;
await page.exposeFunction("testnetWalletRequest", async ({ method, params = [] }) => {
  if (method === "eth_requestAccounts" || method === "eth_accounts") return [account.address];
  if (method === "eth_chainId") return "0x14a34";
  if (method === "eth_getCode") return (await rpc.getCode({ address: params[0] })) || "0x";
  if (method === "personal_sign") {
    loginSignatures++;
    return account.signMessage({ message: { raw: params[0] } });
  }
  if (method === "eth_signTypedData_v4") {
    if (!["approve", "purchase"].includes(input.action)) throw new Error("This check does not authorize payment signing");
    const data = JSON.parse(params[1]);
    if (Number(data.domain.chainId) !== 84532 || data.domain.verifyingContract.toLowerCase() !== asset.toLowerCase()
        || data.primaryType !== "TransferWithAuthorization" || data.message.from.toLowerCase() !== account.address.toLowerCase()
        || data.message.to.toLowerCase() !== input.payee.toLowerCase()
        || BigInt(data.message.value) > 100000n || BigInt(data.message.value) <= 0n
        || Number(data.message.validBefore) > Math.floor(Date.now() / 1000) + 180
        || paymentSignatures > 0) throw new Error("Testnet signing policy rejected this request");
    paymentSignatures++;
    return account.signTypedData(data);
  }
  throw new Error("Unexpected wallet request");
});
await page.addInitScript(() => {
  const listeners = new Map();
  window.ethereum = {
    request: (args) => window.testnetWalletRequest(args),
    on: (event, listener) => { if (!listeners.has(event)) listeners.set(event, new Set()); listeners.get(event).add(listener); },
    removeListener: (event, listener) => listeners.get(event)?.delete(listener),
  };
});
if (input.action === "recover") {
  if (!input.orderId) throw new Error("Recovery requires the existing order ID");
  await page.addInitScript((id) => localStorage.setItem("hermes-sell:last-order", id), input.orderId);
}
page.on("response", async (response) => {
  const target = new URL(response.url());
  if (target.pathname.startsWith("/v1/orders/") || target.pathname.endsWith("/orders")) {
    try {
      const document = await response.json();
      if (document?.id && document?.order_digest) lastOrder = document;
    } catch {}
  }
});
try {
  await page.goto(input.url);
  const connect = page.getByRole("button", { name: input.action === "recover" ? "Reconnect and restore order" : "Connect wallet", exact: true });
  await connect.click();
  if (input.action === "recover") {
    await page.getByRole("heading", { name: "Result", exact: true }).waitFor({ timeout: 20000 });
    if (lastOrder?.id !== input.orderId || lastOrder?.payment !== "settled" || paymentSignatures !== 0) {
      throw new Error("The original settled order was not recovered without signing");
    }
  } else if (input.action === "connect") {
    await page.getByText("Wallet connected", { exact: true }).waitFor({ timeout: 20000 });
  } else if (input.action === "approve") {
    await page.getByRole("button", { name: "Approve 0.10 USDC for agent", exact: true }).click();
    await page.getByText("Approved. Ask your agent to submit this purchase.").waitFor({ timeout: 20000 });
  } else {
    await page.getByRole("button", { name: "Review quote", exact: true }).click();
    await page.getByRole("button", { name: "Approve 0.10 USDC on testnet", exact: true }).click();
    await page.getByRole("heading", { name: "Result", exact: true }).waitFor({ timeout: 120000 });
    if (lastOrder?.payment !== "settled") throw new Error("The browser did not receive a settled order");
    await page.reload();
    await page.getByRole("button", { name: "Reconnect and restore order" }).click();
    await page.getByRole("heading", { name: "Result", exact: true }).waitFor({ timeout: 20000 });
  }
  if (input.screenshot) await page.screenshot({ path: input.screenshot, fullPage: true });
  console.log(JSON.stringify({ success: true, action: input.action, address: account.address,
    loginSignatures, paymentSignatures, order: lastOrder }));
} catch (error) {
  const alerts = await page.getByRole("alert").allTextContents().catch(() => []);
  console.log(JSON.stringify({ success: false, action: input.action, loginSignatures, paymentSignatures,
    error: error.name, alerts, order: lastOrder }));
  process.exitCode = 1;
} finally {
  await browser.close();
}
