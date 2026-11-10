import type { PaymentPayload, PaymentRequirements } from "@x402/core/types";
import { authorizationTypes, type ExactEIP3009Payload } from "@x402/evm";
import type { SIWxPayload } from "@x402/extensions/sign-in-with-x";
import {
  createWalletClient,
  custom,
  getAddress,
  recoverMessageAddress,
  recoverTypedDataAddress,
  type Address,
  type EIP1193Provider,
} from "viem";
import { baseSepolia } from "viem/chains";
import { createSiweMessage } from "viem/siwe";
import { pretty, request, type Order, type UiConfig } from "./api";

export const NETWORK = "eip155:84532";
export const ASSET = "0x036CbD53842c5426634e7929541eC2318f3dCF7e";
const LOGIN_STATEMENT =
  "Sign in to Hermes Sell to view your orders. This does not authorize a payment.";

export type InjectedProvider = {
  request: (args: { method: string; params?: unknown[] }) => Promise<unknown>;
  on?: (event: string, listener: (...args: unknown[]) => void) => void;
  removeListener?: (
    event: string,
    listener: (...args: unknown[]) => void,
  ) => void;
};
export type ConnectedWallet = {
  provider: InjectedProvider;
  address: Address;
};
type Challenge = {
  nonce: string;
  message: string;
  payload: Omit<SIWxPayload, "signature">;
  expires_at: number;
};
type WalletSession = {
  token?: string;
  connected?: boolean;
  owner: string;
  address: string;
  network: string;
  expires_at: number;
};

export function sellerOrigin(config: UiConfig) {
  const origin = config.role === "buyer" ? config.seller_origin : config.origin;
  if (!origin) throw new Error("The seller origin is missing.");
  const url = new URL(origin);
  if (url.origin !== origin || !["http:", "https:"].includes(url.protocol))
    throw new Error("The seller origin is invalid.");
  if (
    url.protocol !== "https:" &&
    !["127.0.0.1", "localhost", "[::1]"].includes(url.hostname)
  )
    throw new Error("Remote sellers must use HTTPS.");
  return origin;
}

function sameAddress(left: string | undefined, right: string) {
  return typeof left === "string" && left.toLowerCase() === right.toLowerCase();
}

export function assertTestnetConfig(config: UiConfig) {
  if (
    config.mode !== "testnet" ||
    config.auth !== "siwx" ||
    config.origin !== location.origin ||
    config.network !== NETWORK ||
    !sameAddress(config.asset, ASSET)
  )
    throw new Error(
      "This storefront is not configured for Base Sepolia wallet payments.",
    );
  sellerOrigin(config);
}

export async function assertWallet(wallet: ConnectedWallet) {
  const [accounts, chain] = await Promise.all([
    wallet.provider.request({ method: "eth_accounts" }),
    wallet.provider.request({ method: "eth_chainId" }),
  ]);
  if (
    !Array.isArray(accounts) ||
    typeof accounts[0] !== "string" ||
    !sameAddress(accounts[0], wallet.address)
  )
    throw new Error(
      "The wallet account changed. Reconnect the original wallet.",
    );
  if (typeof chain !== "string" || BigInt(chain) !== 84532n)
    throw new Error("Switch your wallet to Base Sepolia, then reconnect.");
}

export async function connectWallet(
  config: UiConfig,
): Promise<ConnectedWallet> {
  assertTestnetConfig(config);
  const provider = (window as Window & { ethereum?: InjectedProvider })
    .ethereum;
  if (!provider?.request)
    throw new Error(
      "Install an EVM browser wallet to connect on Base Sepolia.",
    );
  const accounts = await provider.request({ method: "eth_requestAccounts" });
  if (!Array.isArray(accounts) || typeof accounts[0] !== "string")
    throw new Error("Select a wallet account to continue.");
  const wallet = { provider, address: getAddress(accounts[0]) };
  await assertWallet(wallet);
  const code = await provider.request({
    method: "eth_getCode",
    params: [wallet.address, "latest"],
  });
  if (code !== "0x")
    throw new Error(
      "Use an externally owned account. Smart accounts are not supported.",
    );
  return wallet;
}

function client(wallet: ConnectedWallet) {
  return createWalletClient({
    account: wallet.address,
    chain: baseSepolia,
    transport: custom(wallet.provider as EIP1193Provider),
  });
}

function signInMessage(payload: Omit<SIWxPayload, "signature">) {
  const issuedAt = new Date(payload.issuedAt);
  const expirationTime = new Date(payload.expirationTime!);
  // Preserve the server's exact RFC3339 timestamps. Viem otherwise adds .000
  // milliseconds, which changes the bytes signed by the Python SIWX verifier.
  return createSiweMessage({
    domain: payload.domain,
    address: getAddress(payload.address),
    uri: payload.uri,
    version: "1",
    chainId: 84532,
    nonce: payload.nonce,
    statement: payload.statement,
    issuedAt,
    expirationTime,
  })
    .replace(
      "Issued At: " + issuedAt.toISOString(),
      "Issued At: " + payload.issuedAt,
    )
    .replace(
      "Expiration Time: " + expirationTime.toISOString(),
      "Expiration Time: " + payload.expirationTime,
    );
}

export async function signIn(wallet: ConnectedWallet, config: UiConfig) {
  assertTestnetConfig(config);
  await assertWallet(wallet);
  const buyer = config.role === "buyer";
  const origin = sellerOrigin(config);
  const challenge = await request<Challenge>(
    buyer ? "/buyer/auth/challenge" : "/auth/challenge",
    {
      method: "POST",
      body: pretty({ address: wallet.address }),
    },
  );
  const payload = challenge.payload;
  const issued = Date.parse(payload.issuedAt);
  const expires = Date.parse(payload.expirationTime || "");
  if (
    payload.domain !== new URL(origin).host ||
    payload.uri !== origin + "/auth/verify" ||
    !sameAddress(payload.address, wallet.address) ||
    payload.chainId !== NETWORK ||
    payload.type !== "eip191" ||
    payload.signatureScheme !== "eip191" ||
    payload.version !== "1" ||
    payload.statement !== LOGIN_STATEMENT ||
    payload.nonce !== challenge.nonce ||
    !/^[a-f0-9]{64}$/i.test(payload.nonce) ||
    !Number.isFinite(issued) ||
    !Number.isFinite(expires) ||
    issued > Date.now() + 30_000 ||
    expires <= Date.now() ||
    expires - issued > 300_000 ||
    expires !== challenge.expires_at * 1000 ||
    payload.resources ||
    payload.notBefore ||
    payload.requestId ||
    signInMessage(payload) !== challenge.message
  )
    throw new Error(
      "The wallet sign-in challenge does not match this storefront.",
    );
  const signature = await client(wallet).signMessage({
    message: challenge.message,
  });
  if (
    !sameAddress(
      await recoverMessageAddress({ message: challenge.message, signature }),
      wallet.address,
    )
  )
    throw new Error(
      "The wallet returned a signature from a different account.",
    );
  await assertWallet(wallet);
  const session = await request<WalletSession>(
    buyer ? "/buyer/auth/verify" : "/auth/verify",
    {
      method: "POST",
      body: pretty({ payload: { ...payload, signature } }),
    },
  );
  if (
    !sameAddress(session.owner, wallet.address) ||
    !sameAddress(session.address, wallet.address) ||
    session.network !== NETWORK ||
    (buyer
      ? session.connected !== true
      : typeof session.token !== "string" || !session.token) ||
    session.expires_at * 1000 <= Date.now()
  )
    throw new Error(
      "The sign-in response does not match the connected wallet.",
    );
  return session;
}

export function paymentRequirements(
  order: Order,
  config: UiConfig,
): PaymentRequirements {
  assertTestnetConfig(config);
  const requirement = order.payment_requirements;
  if (
    order.mode !== "testnet" ||
    !order.owner ||
    !/^0x[0-9a-fA-F]{40}$/.test(order.owner) ||
    order.origin !== sellerOrigin(config) ||
    order.network !== NETWORK ||
    !sameAddress(order.asset, ASSET) ||
    !/^\d{1,8}$/.test(order.amount) ||
    BigInt(order.amount) < 1n ||
    BigInt(order.amount) > 10_000_000n ||
    !/^[a-f0-9]{64}$/.test(order.order_digest) ||
    order.payment_nonce !== "0x" + order.order_digest ||
    !requirement ||
    requirement.scheme !== "exact" ||
    requirement.network !== NETWORK ||
    !sameAddress(requirement.asset, ASSET) ||
    !sameAddress(requirement.payTo, order.pay_to) ||
    requirement.amount !== order.amount ||
    requirement.maxTimeoutSeconds !== 180 ||
    requirement.extra?.name !== "USDC" ||
    requirement.extra?.version !== "2" ||
    requirement.extra?.assetTransferMethod !== "eip3009" ||
    order.quote_expires_at * 1000 <= Date.now()
  )
    throw new Error(
      "The payment requirements do not match this Base Sepolia quote.",
    );
  getAddress(order.pay_to);
  return requirement;
}

export async function signPayment(
  wallet: ConnectedWallet,
  order: Order,
  config: UiConfig,
): Promise<PaymentPayload> {
  const requirement = paymentRequirements(order, config);
  await assertWallet(wallet);
  paymentRequirements(order, config);
  if (order.owner && !sameAddress(order.owner, wallet.address))
    throw new Error("Reconnect the wallet that created this order.");
  const authorization: ExactEIP3009Payload["authorization"] = {
    from: wallet.address,
    to: getAddress(order.pay_to),
    value: order.amount,
    validAfter: "0",
    validBefore: String(
      Math.floor(Date.now() / 1000) + requirement.maxTimeoutSeconds,
    ),
    nonce: order.payment_nonce!,
  };
  // The SDK generates a random nonce. Its canonical EIP-3009 types are reused
  // here so the signed nonce binds the exact order, including its origin/input.
  const typedData = {
    domain: {
      name: "USDC",
      version: "2",
      chainId: 84532,
      verifyingContract: ASSET as Address,
    },
    types: authorizationTypes,
    primaryType: "TransferWithAuthorization" as const,
    message: {
      ...authorization,
      value: BigInt(authorization.value),
      validAfter: BigInt(authorization.validAfter),
      validBefore: BigInt(authorization.validBefore),
    },
  };
  const signature = await client(wallet).signTypedData(typedData);
  if (
    !sameAddress(
      await recoverTypedDataAddress({ ...typedData, signature }),
      wallet.address,
    )
  )
    throw new Error(
      "The wallet returned a payment signature from a different account.",
    );
  return {
    x402Version: 2,
    accepted: structuredClone(requirement),
    resource: { url: order.origin + "/v1/orders/" + order.id },
    payload: { authorization, signature },
  };
}

export function wasRejected(error: unknown): boolean {
  if (!error || typeof error !== "object") return false;
  const value = error as { code?: number; cause?: unknown };
  return (
    value.code === 4001 || (value.cause !== error && wasRejected(value.cause))
  );
}

export type ReviewChallenge = {
  challenge_id: string;
  mode: "simulation" | "testnet";
  message: string;
  payload: {
    directory_origin: string;
    listing_id: string;
    order_id: string;
    payer: string;
    score: number;
    comment: string;
    nonce: string;
    expires_at: number;
  };
};

export async function signReview(
  wallet: ConnectedWallet,
  challenge: ReviewChallenge,
  config: UiConfig,
  order: Order,
  score: number,
  comment: string,
) {
  const payload = challenge.payload;
  const bytes = new TextEncoder().encode(
    JSON.stringify([
      "hermes-sell-listing-v1",
      sellerOrigin(config),
      order.slug,
    ]),
  );
  const identity = [
    ...new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)),
  ]
    .map((value) => value.toString(16).padStart(2, "0"))
    .join("");
  const message =
    "Publish a Hermes Sell review\n" +
    `Directory: ${payload.directory_origin}\nListing: ${payload.listing_id}\nOrder: ${payload.order_id}\n` +
    `Rating: ${payload.score}/5\nComment: ${JSON.stringify(payload.comment)}\nWallet: ${payload.payer}\n` +
    `Nonce: ${payload.nonce}\nExpires: ${payload.expires_at}\n` +
    "This signature publishes a review only. It does not authorize a payment.";
  if (
    challenge.mode !== "testnet" ||
    config.mode !== "testnet" ||
    payload.directory_origin !== config.directory_origin ||
    payload.listing_id !== identity ||
    payload.order_id !== order.id ||
    payload.payer !== order.owner ||
    !sameAddress(payload.payer, wallet.address) ||
    payload.score !== score ||
    payload.comment !== comment ||
    challenge.challenge_id !== payload.nonce ||
    !/^[0-9a-f]{64}$/.test(payload.nonce) ||
    !Number.isInteger(payload.expires_at) ||
    payload.expires_at * 1000 <= Date.now() ||
    payload.expires_at * 1000 > Date.now() + 330_000 ||
    message !== challenge.message
  )
    throw new Error(
      "The review signature request does not match this purchase, rating or directory.",
    );
  await assertWallet(wallet);
  const signature = await client(wallet).signMessage({ message });
  await assertWallet(wallet);
  if (
    !sameAddress(
      await recoverMessageAddress({ message, signature }),
      wallet.address,
    )
  )
    throw new Error("The review was signed by a different wallet.");
  return signature;
}
